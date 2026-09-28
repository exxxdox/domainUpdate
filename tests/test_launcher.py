"""启动入口的装配测试。

launcher 本身只做「按顺序调用几个组件」，行为几乎都在被调用的模块里测过了。
这里只盯一件事：**凭据只读一次，并且同一个守卫实例一路传到 serve()**。
少了这一条，serve() 会自己再读一遍环境变量，症状只是启动日志里出现两条「未配置凭据」的
WARNING、且真正生效的是第二个实例——在日志里很难注意到。
"""

from unittest.mock import MagicMock, patch

# launcher.py 在仓库根目录而不是包内（pytest 的 pythonpath = ["."] 让它可以被直接导入）。
from launcher import main


def test_credentials_are_read_once_and_handed_to_serve() -> None:
    guard = MagicMock(name="guard")
    guard.enabled = True
    store = MagicMock(name="store")
    store.data_dir = ".data"
    store.load.return_value = MagicMock(ok=False, message="尚未配置")
    serve = MagicMock(name="serve", return_value=0)

    with (
        patch("launcher.configure_logging"),
        patch("launcher.configure_thread_stack"),
        patch("launcher.ConfigStore", return_value=store),
        patch("launcher.resolve_web_port", return_value=8501),
        patch("launcher.AuthGuard") as auth_guard,
        patch("launcher.missing_credential_variables", return_value=()),
        patch("launcher.get_scheduler"),
        patch("launcher.serve", serve),
    ):
        auth_guard.from_env.return_value = guard

        assert main() == 0

    # 环境变量只读一次。
    assert auth_guard.from_env.call_count == 1
    # 传下去的必须是同一个实例：serve() 拿到 None 就会自己再造一个守卫。
    assert serve.call_args.kwargs["auth"] is guard


def test_incomplete_credentials_stop_the_launcher() -> None:
    """只配一半（多半是变量名写错）必须拒绝启动。

    按「未配置」放行的后果是：以为开了登录，实际控制台完全裸奔，而全库只有一行 WARNING。
    """
    serve = MagicMock(name="serve", return_value=0)

    with (
        patch("launcher.configure_logging"),
        patch("launcher.configure_thread_stack"),
        patch("launcher.missing_credential_variables", return_value=("WEB_PASSWORD",)),
        patch("launcher.AuthGuard") as auth_guard,
        patch("launcher.serve", serve),
    ):
        assert main() != 0

    serve.assert_not_called()
    auth_guard.from_env.assert_not_called()
