"""
Dashboard UI Template Module.
Renders the primary web interface for hfsyncenc.
"""
from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parent / "dashboard.html.template"
_TEMPLATE_CACHE = None


def get_template_text() -> str:
    """Loads and caches the raw dashboard HTML template."""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        with open(_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            _TEMPLATE_CACHE = f.read()
    return _TEMPLATE_CACHE


def render_dashboard(
    primary_user: str,
    total_accounts: int,
    total_repos: int,
    total_files: int,
    total_cached_mb_str: str,
    total_size_str: str,
    jelly_badge_cls: str,
    jelly_badge_text: str,
    skipped_stat_html: str,
    skipped_banner_html: str,
    files_json_str: str,
    skipped_json_str: str,
) -> str:
    """Renders the dashboard with the dynamic context parameters."""
    template = get_template_text()
    return template.format(
        primary_user=primary_user,
        total_accounts=total_accounts,
        total_repos=total_repos,
        total_files=total_files,
        total_cached_mb_str=total_cached_mb_str,
        total_size_str=total_size_str,
        jelly_badge_cls=jelly_badge_cls,
        jelly_badge_text=jelly_badge_text,
        skipped_stat_html=skipped_stat_html,
        skipped_banner_html=skipped_banner_html,
        files_json_str=files_json_str,
        skipped_json_str=skipped_json_str,
    )
