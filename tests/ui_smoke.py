"""웹 UI 스모크 테스트 - Playwright(크로미움)로 실제 서버를 sim 모드로 띄우고
로그인한 뒤, 16개 화면을 모바일(390x844)·데스크톱(1440x900) 두 크기에서 전부
열어 콘솔 오류·가로 스크롤이 없는지 확인한다.

`python tests/ui_smoke.py` 로 실행한다.

★ playwright 패키지가 없거나(설치는 requirements-dev.txt), 크로미움 실행 파일이
없으면(별도로 `playwright install chromium` 필요) 실패로 보지 않고 안내만 남기고
종료 코드 0으로 건너뛴다 - CI 매트릭스 중 브라우저를 안 깐 곳에서 전체 빌드를
빨갛게 만들면 안 된다.

★ 서버는 이 저장소를 임시 폴더로 통째로 복사한 뒤 그 사본에서 띄운다 - 실행 중
생기는 state/·logs/·config.yaml 변경이 실제 작업 폴더를 더럽히면 안 된다.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright 가 설치돼 있지 않아 tests/ui_smoke.py 를 건너뜁니다 "
          "(pip install -r requirements-dev.txt 로 설치할 수 있습니다).")
    sys.exit(0)

import requests  # noqa: E402

_total = 0
_failures: list = []

TABS = [
    "dash", "selection", "news", "market", "perf", "journal", "marketreview", "about",
    "review", "lab", "playbook", "rules", "release", "setup", "themes", "config",
]
VIEWPORTS = [("모바일 390x844", 390, 844), ("데스크톱 1440x900", 1440, 900)]
DEFAULT_AUTH_PASSWORD = "123456"  # daytrader/server.py 의 DEFAULT_AUTH_PASSWORD 와 같은 값.


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    mark = "OK  " if cond else "FAIL"
    line = f"[{mark}] {name}"
    if extra:
        line += f" - {extra}"
    print(line)
    if not cond:
        _failures.append(name)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _copy_repo(dest: str) -> None:
    skip_names = {".git", "logs", "state", "__pycache__", ".pytest_cache", "themes.yaml.bak", "config.yaml.bak"}

    def _ignore(_dirpath, names):
        return [n for n in names if n in skip_names or n.endswith((".pyc", ".pyo"))]

    shutil.copytree(ROOT, dest, ignore=_ignore)

    # ★ sim 모드로 강제한다 - 이 화면은 시뮬레이션 위에서만 안전하게 여러 번
    # 열고 닫을 수 있다(실거래·모의주문 계좌로 잘못 붙는 걸 막는다).
    cfg_path = os.path.join(dest, "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        text = f.read()
    text, n = re.subn(r"(?m)^mode:.*$", "mode: sim", text, count=1)
    if n == 0:
        text = "mode: sim\n" + text
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(text)


def _read_password(repo_dir: str) -> str:
    """실제 서버와 같은 규칙(환경변수 > secrets.yaml > 기본값)으로 비밀번호를
    읽는다. secrets.py 는 값이 암호화돼 저장될 수 있어(secrets.encrypt) 직접
    YAML 을 읽지 않고, 복사본의 daytrader.secrets 모듈을 그대로 불러 쓴다."""
    env_val = os.environ.get("DAYTRADER_AUTH_PASSWORD", "").strip()
    if env_val:
        return env_val

    removed = [name for name in list(sys.modules) if name == "daytrader" or name.startswith("daytrader.")]
    for name in removed:
        del sys.modules[name]
    sys.path.insert(0, repo_dir)
    try:
        from daytrader import secrets as secrets_mod
        return secrets_mod.get("auth_password") or DEFAULT_AUTH_PASSWORD
    except Exception as exc:
        print(f"secrets.yaml 에서 비밀번호를 읽지 못해 기본값을 씁니다: {exc}")
        return DEFAULT_AUTH_PASSWORD
    finally:
        sys.path.remove(repo_dir)
        for name in list(sys.modules):
            if name == "daytrader" or name.startswith("daytrader."):
                del sys.modules[name]


def _drain_output(proc: subprocess.Popen, buf: "collections.deque[str]") -> None:
    """★★★ 실제로 겪은 문제 - 서버 로그(특히 [4-1]에서 실패마다 exc_info=True 로
    남기는 traceback)가 이 자식 프로세스의 stdout 파이프 버퍼(리눅스 기본
    64KB)를 채우면, 아무도 그 파이프를 읽어가지 않는 한 다음 로그를 쓰려는
    쪽이 write() 에서 영영 멈춘다 - 그 write 가 로깅 락을 쥔 채로 멈추면
    같은 락을 기다리는 다른 스레드까지 전부 멎고, 결국 이벤트 루프 자체가
    응답을 멈춘다(브라우저 쪽에서는 그냥 "서버가 안 뜬다"로만 보였다).
    파이프를 계속 읽어서 버려야(최근 N줄만 버퍼에 남기고) 이 교착을 막는다."""
    try:
        for line in proc.stdout:
            buf.append(line)
    except Exception:
        pass


def _wait_for_server(base_url: str, proc: subprocess.Popen, out_buf: "collections.deque[str]",
                      timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"서버 프로세스가 먼저 끝났습니다(종료코드 {proc.returncode}):\n{''.join(out_buf)}")
        try:
            r = requests.get(base_url + "/api/auth/check", timeout=1)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"서버가 시간 안에 뜨지 않았습니다.\n{''.join(out_buf)}")


def _goto_tab(page, base_url: str, tab: str) -> None:
    """탭마다 완전히 새로 불러온다(해시만 바꾸는 SPA 네비게이션 대신).
    ★★★ 실제로 겪은 문제 - 대시보드에는 매매 상태 카드마다 5초 간격
    setInterval 이 있고(_controlTimers), 해외주식 카드는 엔진이 꺼져 있으면
    매번 daytrader.market.snapshot() 을 그 자리에서(동기로, 이벤트 루프를
    막으며) 다시 계산한다 - 대시보드에 오래 머물수록 이런 폴링이 쌓여
    서버 전체가 응답을 멈추는 순간이 있었다(설정 재확인 POST 가 30초
    넘게 응답을 못 받음). 탭마다 페이지를 완전히 새로 열면 이전 탭의
    폴링 타이머가 그때그때 사라져 이 문제를 비켜 간다 - 화면이 실제로
    안고 있는 성능 문제 자체를 고치는 건 이 스모크 테스트의 범위 밖이다.
    캐시 버스터 쿼리(_t)를 붙여야 해시만 바뀌는 "같은 문서" 내비게이션으로
    취급되지 않고 실제로 다시 불러온다."""
    url = f"{base_url}/?_t={time.time_ns()}#{tab}"
    page.goto(url, wait_until="load", timeout=60000)
    # 로그인 유지 확인 후 - [설정] 그룹 첫 진입이면 재확인 모달이, 아니면 그
    # 탭 패널 자체가 뜬다.
    page.wait_for_function(
        """(id) => {
            const overlay = document.getElementById('settings-guard-overlay');
            const overlayOpen = !!overlay && getComputedStyle(overlay).display !== 'none';
            const panel = document.querySelector('.panel[data-panel="' + id + '"]');
            const panelVisible = !!panel && getComputedStyle(panel).display !== 'none';
            return overlayOpen || panelVisible;
        }""",
        arg=tab, timeout=45000,
    )


def _maybe_confirm_settings(page, password: str) -> None:
    """[설정] 그룹 화면(준비·연결/테마/설정)에 처음 들어가면 비밀번호 재확인
    모달(#settings-guard-overlay)이 뜬다 - 떠 있으면 채우고 통과시킨다."""
    display = page.evaluate(
        "() => { const o = document.getElementById('settings-guard-overlay'); "
        "return o ? getComputedStyle(o).display : 'none'; }"
    )
    if display == "none":
        return
    page.fill("#settings-guard-password", password)
    page.click("#settings-guard-form button[type=submit]")
    page.wait_for_function(
        "() => { const o = document.getElementById('settings-guard-overlay'); "
        "return !o || getComputedStyle(o).display === 'none'; }",
        timeout=10000,
    )


def _wait_panel_ready(page, tab: str, timeout_ms: int = 45000) -> None:
    """패널이 비어 있지 않고(내용이 그려졌고) 로딩 스켈레톤이 남아있지 않을
    때까지 기다린다. 시간 안에 안 끝나도 실패로 보지 않는다 - 그 시점까지
    그려진 화면 그대로 콘솔 오류·가로 스크롤을 검사한다."""
    try:
        page.wait_for_function(
            """(id) => {
                const p = document.querySelector('.panel[data-panel="' + id + '"]');
                return !!p && p.children.length > 0 && !p.querySelector('.skel');
            }""",
            arg=tab, timeout=timeout_ms,
        )
    except Exception:
        pass


def _run_checks(browser, base_url: str, password: str) -> None:
    page = browser.new_page()
    console_errors: list = []
    page.on("console", lambda msg: console_errors.append(f"[console] {msg.text}") if msg.type == "error" else None)
    page.on("pageerror", lambda exc: console_errors.append(f"[pageerror] {exc}"))

    page.set_viewport_size({"width": 1440, "height": 900})
    # ★★★ 실제로 겪은 문제 - 대시보드는 뜨자마자 여러 카드가 동시에
    # /api/overseas/status 를 부르는데, 해외주식 엔진이 꺼져 있으면 이 라우트가
    # 매번(첫 호출은 캐시가 비어) daytrader.market.snapshot() 을 그 자리에서
    # 동기로 다시 계산한다 - async 라우트 안에서 동기로 실행되다 보니 이벤트
    # 루프 전체가 막힌다. 이 샌드박스처럼 외부 네트워크가 막혀 있으면 그 첫
    # 호출들이 몰려 서버 전체가 완전히 멎는 걸 실제로 봤다(비밀번호 재확인
    # 요청도 응답을 못 받았다). 화면 코드 자체를 고치는 건 이 스모크 테스트의
    # 범위 밖이라, 로그인 직후 대시보드가 아니라 네트워크를 안 쓰는 "소개"
    # 화면으로 먼저 들어가 그 몰림을 피하고, 캐시(10분)를 요청 하나로 미리
    # 데운 뒤에야 대시보드를 포함한 나머지 화면을 돈다.
    page.goto(base_url + "/#about", wait_until="load")

    page.wait_for_selector("#login-password", state="visible", timeout=15000)
    page.fill("#login-password", password)
    console_errors.clear()  # 로그인 폼 자체의 오류만 남긴다.
    page.click("#login-form button[type=submit]")
    page.wait_for_selector('.panel[data-panel="about"]', state="visible", timeout=15000)
    check("로그인 성공(비밀번호 확인 후 화면 진입)", not console_errors, "; ".join(console_errors[:3]))

    try:
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        requests.get(base_url + "/api/overseas/status", cookies=cookies, timeout=30)
    except requests.RequestException as exc:
        print(f"환율 캐시 예열 실패(계속 진행): {exc}")

    for viewport_label, w, h in VIEWPORTS:
        page.set_viewport_size({"width": w, "height": h})
        for tab in TABS:
            console_errors.clear()
            _goto_tab(page, base_url, tab)
            _maybe_confirm_settings(page, password)
            page.wait_for_selector(f'.panel[data-panel="{tab}"]', state="visible", timeout=45000)
            _wait_panel_ready(page, tab)

            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
            )
            label = f"{tab} @ {viewport_label}"
            check(f"{label}: 콘솔 오류 없음", not console_errors, "; ".join(console_errors[:3]))
            check(f"{label}: 가로 스크롤 없음", not overflow,
                  f"scrollWidth={page.evaluate('document.documentElement.scrollWidth')} "
                  f"clientWidth={page.evaluate('document.documentElement.clientWidth')}")

    page.close()


def main() -> None:
    try:
        pw_ctx = sync_playwright().start()
    except Exception as exc:
        print(f"playwright 를 시작하지 못해 건너뜁니다: {exc}")
        sys.exit(0)

    try:
        try:
            browser = pw_ctx.chromium.launch()
        except Exception as exc:
            print(f"크로미움 실행 파일을 찾지 못해 건너뜁니다({exc}). "
                  "'playwright install chromium' 이 필요할 수 있습니다.")
            sys.exit(0)

        tmpdir = tempfile.mkdtemp(prefix="adt-ui-smoke-")
        proc = None
        try:
            repo_dir = os.path.join(tmpdir, "repo")
            _copy_repo(repo_dir)
            password = _read_password(repo_dir)
            port = _free_port()
            base_url = f"http://127.0.0.1:{port}"

            env = os.environ.copy()
            env["DAYTRADER_HOST"] = "127.0.0.1"
            env["DAYTRADER_PORT"] = str(port)
            proc = subprocess.Popen(
                [sys.executable, "run.py", "ui"],
                cwd=repo_dir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            out_buf: "collections.deque[str]" = collections.deque(maxlen=500)
            threading.Thread(target=_drain_output, args=(proc, out_buf), daemon=True).start()
            _wait_for_server(base_url, proc, out_buf)
            _run_checks(browser, base_url, password)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            browser.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
    finally:
        pw_ctx.stop()

    print(f"\n총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        print("실패한 검증:")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
