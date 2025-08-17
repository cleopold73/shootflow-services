# python
from pathlib import Path
from dynaconf import Dynaconf


def _find_root_with_settings(start: Path) -> Path | None:
    """Walk upwards from start until a directory containing settings.toml is found."""
    for p in (start, *start.parents):
        if (p / "settings.toml").exists():
            return p
    return None


# Prefer the directory that contains this module; fall back to CWD search if needed
_here = Path(__file__).resolve()
base_dir = _find_root_with_settings(_here) or _find_root_with_settings(Path.cwd()) or _here.parent.parent.parent

# Configure Dynaconf to resolve files relative to base_dir
settings = Dynaconf(
    root_path=str(base_dir),
    settings_files=("settings.toml", ".secrets.toml"),  # resolved under root_path
    environments=True,                                  # supports [default]/[development]/[production]
    envvar_prefix="SHOOTFLOW",                          # env overrides like SHOOTFLOW_AWS_REGION
    env_switcher="ENV_FOR_DYNACONF",                    # select environment for tests or runtime
    load_dotenv=True,
    merge_enabled=True,
)

# Example access patterns:
# - Top level: settings.get("AWS_REGION")
# - Namespaced [exif] section (flattened): settings.get("EXIF_BLOCKLIST"), settings.get("EXIF_MAX_TEXT_LENGTH", cast=int)