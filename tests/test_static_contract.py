"""静态资源之间的契约测试。

项目没有前端测试框架，而前端最贵的两类错误都恰好可以静态发现：
拼错的元素 id 会让初始化整段不执行（表现为白屏），内联样式/脚本会被
CSP 静默丢弃（浏览器只把违规写进控制台，页面本身看不出问题）。
这两类错误在浏览器里都很难定位，所以在这里挡住。

不引入 jsdom：它不执行 CSP，恰好验证不了上面第二类。
"""

import re
from pathlib import Path

from domain_update.web.server import STATIC_DIR, STATIC_FILES

INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
APP_CSS = STATIC_DIR / "app.css"

# el("some-id")：app.js 取元素 id 的唯一入口。
_EL_CALL = re.compile(r"""\bel\(\s*["']([^"']+)["']\s*\)""")
_ID_ATTR = re.compile(r"""\bid=["']([^"']+)["']""")

# CSP 禁止的三类写法。style-src/script-src 都是 'self'，没有 'unsafe-inline'。
_INLINE_STYLE_ATTR = re.compile(r"""\sstyle\s*=""")
_INLINE_EVENT_ATTR = re.compile(r"""\son[a-z]+\s*=""")
_INLINE_STYLE_TAG = re.compile(r"<style[\s>]")
_SCRIPT_WITHOUT_SRC = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>")

# 只关心站内绝对路径；favicon 是 data: URI，不该出现在 STATIC_FILES 里。
_ASSET_REF = re.compile(r"""(?:href|src)=["'](/[^"']*)["']""")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_every_element_id_used_by_app_js_exists_in_index_html() -> None:
    """app.js 里 el() 取不到的 id 会抛错，而 init() 末尾的 refresh() 在它之后。

    一个拼错的 id 会把「按钮不灵」放大成整页白屏，且控制台只有一行 TypeError。
    """
    used = set(_EL_CALL.findall(_read(APP_JS)))
    defined = set(_ID_ATTR.findall(_read(INDEX_HTML)))

    assert used <= defined, f"index.html 缺少这些 id：{sorted(used - defined)}"


def test_static_assets_have_no_inline_style_or_script() -> None:
    """CSP 是 default-src 'none'，内联样式与内联事件会被浏览器直接丢弃。

    丢弃是静默的：页面只是「看起来没生效」，只有控制台里有 violation 记录。
    """
    offenders: list[str] = []
    for path in (INDEX_HTML, APP_JS, APP_CSS):
        text = _read(path)
        for pattern, label in (
            (_INLINE_STYLE_ATTR, "内联 style 属性"),
            (_INLINE_EVENT_ATTR, "内联事件属性"),
            (_INLINE_STYLE_TAG, "内联 <style> 块"),
            (_SCRIPT_WITHOUT_SRC, "无 src 的 <script>"),
        ):
            if pattern.search(text):
                offenders.append(f"{path.name}: {label}")

    assert not offenders, f"会被 CSP 丢弃的写法：{offenders}"


def test_static_files_map_covers_referenced_assets() -> None:
    """STATIC_FILES 不做首页回落，映射不到的路径直接 404。

    页面引用了一个没登记的静态文件，症状是白屏或样式丢失，排查起来没有线索。
    """
    referenced = set(_ASSET_REF.findall(_read(INDEX_HTML)))

    assert referenced <= set(STATIC_FILES), (
        f"STATIC_FILES 缺少：{sorted(referenced - set(STATIC_FILES))}"
    )
