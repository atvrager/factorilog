"""Render Factorio blueprint strings to PNG images.

This module downloads and builds factorio-blueprint-editor from GitHub.
The module validates the archive checksum and renders blueprints to PNG images.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

# Pinned commit and checksum for factorio-blueprint-editor.
FBE_COMMIT = "62d8b92d1e3ce48218b95043b70889dd35d62439"
FBE_SHA256 = "10bdd7d9ff74944a985f4c517e037ccda55aef1428a58f0787fc522f02a66cd0"
FBE_ARCHIVE_URL = f"https://github.com/Teoxoy/factorio-blueprint-editor/archive/{FBE_COMMIT}.tar.gz"

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_SCRIPT = REPO_ROOT / "tools" / "render_blueprint.js"


def _download_and_verify(url: str, expected_sha256: str, target_file: Path) -> None:
    """Download an archive and verify its SHA256 checksum."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, target_file.open("wb") as out:
        shutil.copyfileobj(response, out)

    hasher = hashlib.sha256()
    with target_file.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    actual_sha256 = hasher.hexdigest()
    if actual_sha256 != expected_sha256:
        target_file.unlink(missing_ok=True)
        raise ValueError(
            f"Checksum mismatch for {url}: expected {expected_sha256}, got {actual_sha256}"
        )


def _extract_tarball(archive_path: Path, dest_dir: Path) -> None:
    """Extract a tarball into the target directory."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        members = []
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) > 1:
                member.name = str(Path(*parts[1:]))
                members.append(member)
        tar.extractall(dest_dir, members=members)


def _patch_website(fbe_dir: Path) -> None:
    """Apply required modifications to the website source."""
    idx_path = fbe_dir / "packages" / "website" / "src" / "index.ts"
    text = idx_path.read_text(encoding="utf-8")
    if "(window as any).editor = editor" not in text:
        text = text.replace(
            "const editor = new Editor()\n",
            "const editor = new Editor()\n;(window as any).editor = editor\n",
        )
    if "decodeURIComponent(p.split('=')[1])" not in text:
        text = text.replace(
            "bpSource = p.split('=')[1]",
            "bpSource = decodeURIComponent(p.split('=')[1])",
        )
    idx_path.write_text(text, encoding="utf-8")


def _build_fbe(fbe_dir: Path) -> None:
    """Install dependencies and build the website bundle."""
    subprocess.run(["npm", "install"], cwd=str(fbe_dir), check=True)
    esbuild_script = fbe_dir / "node_modules" / "esbuild" / "install.js"
    if esbuild_script.is_file():
        subprocess.run(["node", str(esbuild_script)], cwd=str(fbe_dir), check=True)
    subprocess.run(["npm", "install", "puppeteer-core"], cwd=str(fbe_dir), check=True)
    subprocess.run(["npm", "run", "build:website"], cwd=str(fbe_dir), check=True)


def ensure_fbe() -> Path:
    """Ensure the factorio-blueprint-editor assets exist and are compiled.

    Returns:
        Path to the root directory of the factorio-blueprint-editor build.
    """
    if "FACTORILOG_FBE_DIR" in os.environ:
        custom_dir = Path(os.environ["FACTORILOG_FBE_DIR"])
        if (custom_dir / "packages" / "website" / "dist" / "index.html").is_file():
            return custom_dir

    cache_root = (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "factorilog" / "fbe"
    )
    commit_dir = cache_root / FBE_COMMIT
    dist_file = commit_dir / "packages" / "website" / "dist" / "index.html"

    if not dist_file.is_file():
        archive_path = cache_root / f"{FBE_COMMIT}.tar.gz"
        if not archive_path.is_file():
            _download_and_verify(FBE_ARCHIVE_URL, FBE_SHA256, archive_path)
        _extract_tarball(archive_path, commit_dir)
        _patch_website(commit_dir)
        _build_fbe(commit_dir)

    return commit_dir


def render_blueprint(bp_string: str, out_path: str | Path) -> Path:
    """Render a blueprint exchange string to a PNG image file.

    Args:
        bp_string: The Factorio blueprint exchange string.
        out_path: The target file path for the output PNG image.

    Returns:
        The path to the generated PNG image.
    """
    fbe_dir = ensure_fbe()
    dist_dir = fbe_dir / "packages" / "website" / "dist"

    out_file = Path(out_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "NODE_PATH": str(fbe_dir / "node_modules"),
    }
    cmd = [
        "node",
        str(RENDER_SCRIPT),
        bp_string,
        str(out_file),
        str(dist_dir),
    ]
    res = subprocess.run(
        cmd,
        cwd=str(fbe_dir),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Blueprint render failed:\n{res.stderr}\n{res.stdout}")

    return out_file
