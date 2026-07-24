import json
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from platformdirs import user_cache_dir


def main() -> None:
    version = os.environ["CAMOUFOX_VERSION"]
    release = os.environ["CAMOUFOX_RELEASE"]
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "i686",
        "i686": "i686",
        "x86": "i686",
    }
    machine = os.uname().machine.lower()
    arch = arch_map.get(machine)
    if not arch:
        raise SystemExit(f"Unsupported Camoufox arch: {machine}")

    tag = f"v{version}-{release}"
    asset_name = f"camoufox-{version}-{release}-lin.{arch}.zip"
    asset_url = f"https://github.com/daijro/camoufox/releases/download/{tag}/{asset_name}"
    addon_url = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"
    install_root = Path(user_cache_dir("camoufox"))
    version_dir = f"{version}-{release}"
    install_dir = install_root / "browsers" / "official" / version_dir
    temp_dir = Path(tempfile.mkdtemp(prefix="camoufox-install-"))

    try:
        if install_root.exists():
            shutil.rmtree(install_root)
        install_dir.mkdir(parents=True, exist_ok=True)

        archive_path = temp_dir / asset_name
        print(f"Downloading Camoufox package: {asset_url}")
        urllib.request.urlretrieve(asset_url, archive_path)
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(install_dir)

        version_path = install_dir / "version.json"
        version_path.write_text(
            json.dumps(
                {
                    "version": version,
                    "build": release,
                    "release": release,
                    "prerelease": True,
                }
            ),
            encoding="utf-8",
        )

        addon_dir = install_dir / "addons" / "UBO"
        addon_dir.mkdir(parents=True, exist_ok=True)
        addon_path = temp_dir / "ublock-origin.xpi"
        print(f"Downloading default addon UBO: {addon_url}")
        urllib.request.urlretrieve(addon_url, addon_path)
        with zipfile.ZipFile(addon_path) as zf:
            zf.extractall(addon_dir)

        for path in install_root.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644)

        binary = install_dir / "camoufox-bin"
        if binary.exists():
            binary.chmod(0o755)

        # camoufox 0.5.x resolves all resources through the active
        # multiversion entry.  Keep the pinned build active so every launch,
        # including the local solver, is offline and deterministic.
        (install_root / "config.json").write_text(
            json.dumps(
                {
                    "active_version": f"browsers/official/{version_dir}",
                }
            ),
            encoding="utf-8",
        )
        (install_root / ".0.5_FLAG").touch()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
