from __future__ import annotations

import os
import sys

# ★ exe 로 묶으면 __file__ 은 실행이 끝나면 지워질 임시 폴더(_MEIPASS)를 가리킨다.
# 거기에 설정・상태・로그를 쓰면 매번 초기화되고 사용자가 고친 설정도 사라진다.
# exe 만 쓰는 사람에게는 치명적이다. 그래서 "사용자 파일이 사는 곳"과
# "묶여 들어간 읽기 전용 자원이 있는 곳"을 반드시 구분한다.

FROZEN = getattr(sys, "frozen", False)

_USER_FILES = ["config.yaml", "themes.yaml"]


def app_dir() -> str:
    """사용자 파일(설정·상태·로그)이 사는 곳.
    exe 로 묶였으면 exe 가 놓인 폴더, 아니면 프로젝트 루트.
    """
    if FROZEN:
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def res_dir() -> str:
    """묶여 들어간 읽기 전용 자원이 있는 곳.
    exe 로 묶였으면 PyInstaller 가 풀어놓는 임시 폴더(_MEIPASS),
    아니면 프로젝트 루트와 같다.
    """
    if FROZEN:
        return getattr(sys, "_MEIPASS", app_dir())
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_path(*parts: str) -> str:
    return os.path.join(app_dir(), *parts)


def res_path(*parts: str) -> str:
    return os.path.join(res_dir(), *parts)


def ensure_user_files() -> list[str]:
    """exe 첫 실행이면 묶인 config.yaml·themes.yaml 을 exe 옆으로 꺼내 놓는다.
    처음 실행할 때 빈 폴더만 보고 당황하지 않게 하기 위함이다.
    이미 사용자 폴더에 파일이 있으면 절대 덮어쓰지 않는다 - 고친 설정을 지키기 위해서다.
    """
    created: list[str] = []
    if not FROZEN:
        return created
    for name in _USER_FILES:
        dst = app_path(name)
        if os.path.exists(dst):
            continue
        src = res_path(name)
        if not os.path.exists(src):
            continue
        with open(src, "rb") as f_src, open(dst, "wb") as f_dst:
            f_dst.write(f_src.read())
        created.append(dst)
    return created


def config_path() -> str:
    return app_path("config.yaml")


def web_dir() -> str:
    return res_path("web")
