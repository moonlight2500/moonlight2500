#!/usr/bin/env python3
"""트레이 실행기. 프로세스를 둘로 나눈다:
트레이(부모) 아이콘·상태 3초 폴링·종료 / 웹 서비스(자식) HTTP·엔진.

한 프로세스의 스레드에서 uvicorn 을 돌렸더니 실패할 때 예외가 스레드 안에서
사라져 "20초 기다렸다 응답 없음"만 남았다. 별도 프로세스면 종료 코드와
stderr 를 그대로 읽어 보여줄 수 있다.
"""

from __future__ import annotations

import atexit
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ★★★ PyInstaller --windowed(콘솔 없음) 빌드는 sys.stdout/stderr/stdin 이
# 파일 객체가 아니라 그냥 None 이다. uvicorn 은 로그 포맷터를 만들 때
# sys.stdout.isatty() 를 부르는데, None.isatty() 는 AttributeError 로 죽는다
# (실제로 겪은 문제 - "Unable to configure formatter 'default'"). 자식
# 프로세스(--serve)도 이 tray.py 를 거쳐 다시 실행되므로, 여기서 한 번
# None 을 안전한 가짜 스트림으로 바꿔치기해두면 부모·자식 모두에 적용된다.
class _NullStream:
    """콘솔이 없을 때 sys.stdout/stderr/stdin 자리를 채우는 가짜 스트림.
    write/flush 는 아무 일도 안 하고, isatty() 는 항상 False 를 준다 -
    "터미널이 있는 척"을 절대 하지 않는다. 진짜 있는데 없다고 답하는 것보다,
    없는데 없다고 정직하게 답하는 실패가 낫다.
    """

    def write(self, *a, **kw):
        return 0

    def flush(self, *a, **kw):
        pass

    def isatty(self):
        return False

    def read(self, *a, **kw):
        return ""

    def readline(self, *a, **kw):
        return ""

    def fileno(self):
        raise OSError("콘솔이 없는 환경(--windowed)이라 표준 입출력이 없습니다.")


for _stream in ("stdout", "stderr", "stdin"):
    _s = getattr(sys, _stream, None)
    if _s is None:
        setattr(sys, _stream, _NullStream())
    elif hasattr(_s, "reconfigure"):
        # ★★ exe 의 콘솔은 cp949 라 '—' 같은 글자에서 UnicodeEncodeError 로
        # 죽는다. 진단 출력이 죽으면 사용자는 원인을 볼 방법이 없어진다.
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

FROZEN = getattr(sys, "frozen", False)


def _app_dir() -> str:
    """묶인 exe 의 __file__ 은 지워질 임시 폴더를 가리킨다. 거기에 로그를 쓰면 사라진다."""
    if FROZEN:
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
LOG_DIR = os.path.join(APP_DIR, "logs")

# ★ 로컬 요청이 사내 프록시를 거치지 않게 한다.
_local_no_proxy = "127.0.0.1,localhost,::1"
for _key in ("NO_PROXY", "no_proxy"):
    _cur = os.environ.get(_key, "")
    if _local_no_proxy not in _cur:
        os.environ[_key] = ",".join(p for p in (_cur, _local_no_proxy) if p)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log_startup(msg: str) -> None:
    """조용히 죽지 않게 - 실행할 때마다 로그를 남긴다."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "startup.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def alert(title: str, message: str) -> None:
    """메시지 박스로 사용자에게 알린다. 조용히 죽지 않기 위한 3겹 중 하나."""
    _log_startup(f"[alert] {title}: {message}")
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x30)
            return
        except Exception:
            pass
    print(f"[{title}] {message}", file=sys.stderr)


# ━━ 아이콘 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STATE_COLORS = {
    "idle": (110, 113, 120),
    "sim": (60, 74, 99),
    "web": (53, 84, 78),
    "replay": (74, 67, 96),
    "paper": (42, 74, 63),
    "live": (123, 30, 30),
    "halt": (150, 105, 15),
    "degraded": (138, 75, 18),
}


def make_icon(color, badge: bool = False):
    """64×64 RGBA. 오른쪽 위로 향하는 주식 그래프.
    ★ 트레이 아이콘은 16×16 으로 축소돼 표시된다. 형태를 단순하게 유지한다 -
    가는 선과 잔가지는 축소되면 회색 뭉치로 보인다.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([0, 0, 63, 63], radius=14, fill=color)

    white = (255, 255, 255, 255)
    # 막대 4개를 계단처럼 올린다 - 막대는 축소에 잘 견딘다.
    for x1, top in ((10, 45), (21, 38), (32, 30), (43, 22)):
        draw.rectangle([x1, top, x1 + 7, 53], fill=(255, 255, 255, 150))

    # 막대 머리를 잇는 상승 꺾은선 + 화살촉.
    # ★ 좌표를 안쪽으로 당겨야 한다 - 둥근 모서리에 화살촉이 걸치면 잘려서
    # '홈이 파인 것'처럼 보인다.
    draw.line([(13, 42), (24, 35), (35, 27), (45, 19)], fill=white, width=4, joint="curve")
    draw.polygon([(41, 13), (52, 13), (52, 24)], fill=white)

    if badge:
        draw.ellipse([46, 2, 60, 16], fill=(220, 50, 50, 255))

    return img


def band_state(st: dict) -> str:
    """엔진 상태에서 배너/트레이 색상 키를 뽑는다."""
    if st.get("halted"):
        return "halt"
    if st.get("degraded"):
        return "degraded"
    mode = st.get("mode", "idle")
    return mode if mode in STATE_COLORS else "idle"


def summary(st: dict) -> str:
    """트레이 툴팁 문구. ★ 127자로 자른다 (윈도우 제한)."""
    mode = st.get("mode", "idle")
    parts = [f"모드: {mode}"]
    if st.get("halted"):
        parts.append(f"중단: {st.get('halt_reason', '')}")
    elif st.get("degraded"):
        parts.append("시세 저하")
    if "realized_pnl" in st:
        parts.append(f"손익 {st.get('realized_pnl', 0):,.0f}원")
    if "positions" in st:
        pos = st["positions"]
        n = len(pos) if isinstance(pos, dict) else pos
        parts.append(f"보유 {n}")
    return " · ".join(parts)[:127]


# ━━ 레이아웃·환경 점검 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ★ FROZEN 이면 config.yaml/themes.yaml 만 - 묶인 exe 에는 소스 파일이 옆에 없다.
NEEDED_FILES = ["config.yaml", "themes.yaml"] if FROZEN else [
    "config.yaml", "themes.yaml", "run.py",
]


def check_layout() -> list:
    """★ FROZEN 이면 ensure_user_files() 를 먼저 불러 기본 설정을 꺼내 놓는다.
    안 하면 첫 실행에 "파일이 빠졌습니다" 창만 뜬다.
    """
    if FROZEN:
        try:
            from daytrader.paths import ensure_user_files
            ensure_user_files()
        except Exception:
            pass
    return [f for f in NEEDED_FILES if not os.path.exists(os.path.join(APP_DIR, f))]


def looks_like_zip(missing: list) -> bool:
    """경로에 temp/tmp 가 있고 누락 3개 이상이면 탐색기가 ZIP 안에서 그대로 연 것이다."""
    path_lower = APP_DIR.lower()
    return ("temp" in path_lower or "tmp" in path_lower) and len(missing) >= 3


def check_dependencies() -> list:
    missing = []
    for pkg in ("fastapi", "uvicorn"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ★★★ 트레이가 서버를 띄울 때마다 새로 만드는 비밀 토큰 - 서버에 환경변수로 넘기고, 트레이가 서버 API
# (상태 읽기·"매매 중단")를 부를 때 헤더로 보여 준다. 로그인 벽이 생긴 뒤로 이게 없어서 트레이의
# 상태 갱신과 매매 중단 메뉴가 401 로 막혀 있었다. 같은 PC 에서 온 이 토큰만 로그인 없이 인정된다.
LOCAL_TOKEN = ""


def _local_headers(extra: dict | None = None) -> dict:
    h = dict(extra or {})
    if LOCAL_TOKEN:
        h["X-Local-Control"] = LOCAL_TOKEN
    return h


def fetch(url: str, timeout: float = 3.0):
    try:
        req = urllib.request.Request(url, headers=_local_headers())
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def post(url: str, body: dict, timeout: float = 5.0):
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=_local_headers({"Content-Type": "application/json"}), method="POST")
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _log_tail(path: str, n: int = 12) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return "(로그를 읽을 수 없습니다)"


# ━━ 웹 서비스(자식 프로세스) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Server:
    def __init__(self, host: str = "127.0.0.1", port: int = 8000):
        self.host = host
        self.port = port
        self.proc = None
        self.log_path = os.path.join(LOG_DIR, "server.log")

    def _command(self) -> list:
        # ★ FROZEN 이면 자기 자신(exe)을 --serve 로 다시 부른다.
        if FROZEN:
            return [sys.executable, "--serve"]
        return [sys.executable, os.path.join(APP_DIR, "run.py"), "ui"]

    def start(self) -> None:
        os.makedirs(LOG_DIR, exist_ok=True)
        global LOCAL_TOKEN
        import secrets as _secrets
        LOCAL_TOKEN = _secrets.token_hex(24)
        env = os.environ.copy()
        env["DAYTRADER_LOCAL_TOKEN"] = LOCAL_TOKEN
        env["DAYTRADER_HOST"] = self.host
        env["DAYTRADER_PORT"] = str(self.port)
        env["DAYTRADER_PARENT_PID"] = str(os.getpid())
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        log_file = open(self.log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            self._command(), cwd=APP_DIR, env=env,
            stdout=log_file, stderr=subprocess.STDOUT, **kwargs,
        )
        _log_startup(f"서버 시작: pid={self.proc.pid}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout: float = 30):
        """실패 원인을 셋으로 갈라 알린다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                code = self.proc.returncode if self.proc else None
                return False, f"서버 프로세스가 종료되었습니다 (코드 {code}).\n\n{_log_tail(self.log_path)}"
            if tcp_open(self.host, self.port, timeout=0.5):
                # ★★★ 실제로 겪은 문제 - 로그인 기능을 넣으면서 /api/setup 이
                # 비밀번호 없이는 401 을 돌려주게 됐는데, 여기서는 그걸 "서버가
                # 안 떴다"로 오판해서 실제로는 멀쩡히 뜬 서버를 "시작 실패"로
                # 알렸다. /api/auth/check 는 로그인 여부와 무관하게 항상 200을
                # 주므로("떠 있는지"만 확인하는 순수한 헬스체크), 이걸 쓴다.
                data = fetch(f"http://{self.host}:{self.port}/api/auth/check", timeout=2.0)
                if data is not None:
                    return True, ""
                # ★ 포트는 열렸는데 HTTP 응답이 없다 - 프록시 문제의 전형이다.
                return False, (
                    "브라우저에서 직접 열어보세요. 열린다면 프록시·보안 프로그램이 로컬 요청을 "
                    "가로챈 것입니다. 인터넷 옵션에서 '로컬 주소에 프록시 서버 사용 안 함'을 "
                    "켜면 해결됩니다."
                )
            time.sleep(0.3)
        return False, f"포트가 열리지 않았습니다.\n\n{_log_tail(self.log_path)}"

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None


def diagnose() -> dict:
    """파일·패키지·포트·설정·연결(netutil.diagnose)을 한 번에 확인한다."""
    from daytrader import __version__

    result: dict = {"version": __version__}
    result["missing_files"] = check_layout()
    result["missing_packages"] = check_dependencies()
    result["port_in_use"] = port_in_use(int(os.environ.get("DAYTRADER_PORT", "8000")))

    try:
        from daytrader.config import load_config
        cfg = load_config()
        result["config_ok"] = True
        result["mode"] = cfg.mode
    except Exception as exc:
        result["config_ok"] = False
        result["config_error"] = str(exc)

    try:
        from daytrader import netutil
        result["net"] = netutil.diagnose()
    except Exception as exc:
        result["net_error"] = str(exc)

    return result


def _open_browser(port: int) -> None:
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{port}/")


def _open_admin(port: int) -> None:
    """새 기기 승인 관리자 페이지 - 이 트레이(=서버가 도는 이 PC)만 아는 토큰을 URL 에 실어 연다.
    이 토큰 없이는(원격은 물론 같은 PC의 다른 프로그램도) 이 페이지 내용을 볼 수 없다(server.py 의
    _is_admin_local)."""
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{port}/admin?token={LOCAL_TOKEN}")


def _bg(fn):
    """★★ 메뉴 콜백은 반드시 별도 스레드에서 돌린다 (A-15).
    win32 백엔드는 콜백을 메시지 루프 스레드에서 부른다. 거기서 MessageBoxW 를
    띄우거나 HTTP 응답을 기다리면 메시지 펌프가 막혀 트레이가 먹통이 된다.
    """
    def run(*_args):
        threading.Thread(target=fn, name="tray-action", daemon=True).start()
    return run


def run_tray() -> None:
    _log_startup("트레이 시작")

    missing = check_layout()
    if looks_like_zip(missing):
        alert(
            "AutoDayTrading",
            "ZIP 안에서 실행된 것 같습니다.\n\n"
            "탐색기는 ZIP 안의 파일을 더블클릭하면 그 파일 하나만 임시 폴더에 "
            "복사해서 실행합니다. 그래서 나머지 파일을 찾지 못합니다.\n"
            "압축을 먼저 풀고 실행해 주세요.",
        )
        return
    if missing:
        alert("AutoDayTrading", "다음 파일이 없습니다:\n" + "\n".join(missing))
        return

    missing_pkgs = check_dependencies()
    if missing_pkgs:
        alert("AutoDayTrading", "다음 패키지가 설치되어 있지 않습니다: " + ", ".join(missing_pkgs))
        return

    port = int(os.environ.get("DAYTRADER_PORT", "8000"))
    if port_in_use(port):
        # 이미 실행 중일 수 있다 - 없으면 아무 일도 안 하는 것처럼 보이니 반드시 알린다.
        # ★ /api/setup 은 로그인해야 볼 수 있어 여기서는 안 맞다(위 wait_ready() 참고).
        data = fetch(f"http://127.0.0.1:{port}/api/auth/check", timeout=2.0)
        if data is not None:
            alert("AutoDayTrading", "이미 실행 중입니다. 브라우저를 엽니다.")
            _open_browser(port)
        else:
            alert(
                "AutoDayTrading",
                f"포트 {port}가 이미 사용 중입니다. 다른 프로그램이 쓰고 있을 수 있습니다.\n"
                "환경변수 DAYTRADER_PORT 로 다른 포트를 지정해 다시 실행해 보세요 "
                "(예: set DAYTRADER_PORT=8768).",
            )
        return

    # ★★★ 실제로 겪은 문제 - server.py 는 DAYTRADER_HOST 환경변수로 바인딩
    # 주소를 바꿀 수 있다고 스스로 안내하는데(127.0.0.1 이 아니면 경고만
    # 남기고 그대로 연다), 트레이가 자식 프로세스를 띄울 때는 그 값을
    # 무시하고 Server(host=기본값 "127.0.0.1") 로 항상 덮어쓰고 있었다.
    # 그래서 Tailscale 등 외부망에서 접속하려고 DAYTRADER_HOST=0.0.0.0 을
    # 미리 설정해도 조용히 무시되고 항상 localhost 로만 열렸다.
    host = os.environ.get("DAYTRADER_HOST", "127.0.0.1")
    server = Server(host=host, port=port)
    server.start()
    ok, why = server.wait_ready()
    if not ok:
        alert("AutoDayTrading", "서버를 시작하지 못했습니다.\n\n" + why)
        server.stop()
        return

    # ★ 서버가 준비되면 바로 브라우저를 연다. 메뉴에서 "대시보드 열기"를
    # 눌러야만 열리면, 트레이 아이콘을 못 찾은 사용자는 아무 반응도 없다고
    # 오해한다.
    _open_browser(port)

    try:
        import pystray
    except ImportError:
        alert("AutoDayTrading", "pystray 가 설치되어 있지 않아 트레이 없이 웹만 띄웁니다.")
        try:
            server.proc.wait()
        finally:
            server.stop()
        return

    if sys.platform == "win32":
        os.environ.setdefault("PYSTRAY_BACKEND", "win32")

    icon_holder = {"icon": None}

    def _show_dashboard():
        _open_browser(port)

    def _show_status():
        st = fetch(f"http://127.0.0.1:{port}/api/status") or {}
        alert("AutoDayTrading 상태", summary(st))

    def _halt_trading():
        post(f"http://127.0.0.1:{port}/api/engine/stop", {"close_positions": False})
        alert("AutoDayTrading", "매매 중단 신호를 보냈습니다.")

    def _show_admin():
        _open_admin(port)

    def _quit():
        if icon_holder["icon"] is not None:
            icon_holder["icon"].stop()
        server.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("대시보드 열기", _bg(_show_dashboard), default=True),
        pystray.MenuItem("상태 보기", _bg(_show_status)),
        pystray.MenuItem("새 기기 승인 관리", _bg(_show_admin)),
        pystray.MenuItem("매매 중단", _bg(_halt_trading)),
        pystray.MenuItem("종료", _bg(_quit)),
    )

    icon = pystray.Icon("autodaytrading", make_icon(STATE_COLORS["idle"]), "AutoDayTrading", menu)
    icon_holder["icon"] = icon

    def _cleanup():
        server.stop()

    atexit.register(_cleanup)

    def _on_signal(signum, frame):
        _cleanup()
        os._exit(0)

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass

    def _poll():
        last_state = None
        while True:
            time.sleep(3)
            if not server.alive():
                # ★ 여기서 그냥 icon.stop() 만 부르면 트레이가 조용히 사라지고
                # "그냥 종료돼" 로 보고된다 - wait_ready() 를 통과한 뒤에도
                # 서버가 나중에 죽을 수 있다. 반드시 이유를 남긴다.
                code = server.proc.returncode if server.proc else None
                tail = _log_tail(server.log_path)
                _log_startup(f"★ 서버 프로세스가 종료되어(코드 {code}) 트레이도 함께 닫습니다.\n{tail}")
                alert(
                    "AutoDayTrading - 서버가 종료됨",
                    f"서버가 시작된 뒤 알 수 없는 이유로 종료되어(코드 {code}) 트레이도 함께 닫습니다.\n\n"
                    + tail,
                )
                icon.stop()
                return
            st = fetch(f"http://127.0.0.1:{port}/api/status") or {}
            state = band_state(st)
            if state != last_state:
                icon.icon = make_icon(STATE_COLORS.get(state, STATE_COLORS["idle"]))
                last_state = state
            icon.title = summary(st)

    threading.Thread(target=_poll, daemon=True).start()

    # ★ 윈도우 11 은 새 트레이 아이콘을 기본으로 숨긴다.
    _log_startup("트레이 아이콘 등록 - 안 보이면 작업표시줄의 ^ (숨겨진 아이콘)을 확인하세요.")
    icon.run()
    # ★ icon.run() 이 스스로 돌아왔다 - 누가 종료를 눌렀거나 icon.stop() 이
    # 불렸다는 뜻이다. 이유를 알 수 없는 조용한 종료를 다음엔 구분할 수 있게
    # 반드시 한 줄 남긴다.
    _log_startup("트레이 아이콘 루프가 끝났습니다 (종료 메뉴를 눌렀거나 서버 종료 감지).")


def main() -> None:
    if "--serve" in sys.argv:
        from daytrader import server
        server.main()
        return

    if "--check" in sys.argv:
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2, default=str))
        return

    if "--tray-test" in sys.argv:
        import pystray
        icon = pystray.Icon("autodaytrading-test", make_icon(STATE_COLORS["live"]), "빨간 아이콘 테스트(30초)")

        def _stop_later():
            time.sleep(30)
            icon.stop()

        threading.Thread(target=_stop_later, daemon=True).start()
        icon.run()
        return

    # ★★ --windowed 로 빌드하면 콘솔이 없다 - 예외가 나면 트레이스백이 아무
    # 데도 안 남고 프로세스만 조용히 죽는다("그냥 종료돼"). run_tray() 안의
    # 알려진 실패는 각자 alert() 로 안내하지만, 예상 못 한 예외까지 잡으려면
    # 여기서 한 번 더 감싸야 한다 - 이게 마지막 방어선이다.
    try:
        run_tray()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        _log_startup("★ 처리 못 한 예외로 종료됩니다:\n" + tb)
        alert(
            "AutoDayTrading - 예상하지 못한 오류",
            "프로그램이 예상하지 못한 오류로 종료됩니다.\n\n"
            + tb[-1200:]
            + "\n\n이 내용은 logs\\startup.log 에도 남았습니다.",
        )


if __name__ == "__main__":
    main()
