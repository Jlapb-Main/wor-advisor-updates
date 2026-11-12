"""Release a checksummed source build using only this repository's GITHUB_TOKEN."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def gh(*args, capture=False):
    result = subprocess.run(["gh", *args], check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    inputs = event["inputs"]
    version, expected, commit, run = (inputs[k] for k in ("version", "archive_sha256", "source_commit", "source_run"))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version): raise ValueError("Invalid application version")
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not re.fullmatch(r"[0-9a-f]{40}", commit) or not run.isdecimal(): raise ValueError("Invalid build provenance")
    repository = os.environ["GITHUB_REPOSITORY"]
    if repository != "Jlapb-Main/wor-advisor-updates": raise ValueError("Unexpected destination repository")
    tag = "v" + version
    # Never replace assets of an already public immutable release.
    current = subprocess.run(["gh", "release", "view", tag, "--repo", repository, "--json", "isDraft"], capture_output=True, text=True)
    if current.returncode == 0 and not json.loads(current.stdout)["isDraft"]:
        raise ValueError("Release is already public; publish a new version instead")
    url = inputs["artifact_url"]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".oaiusercontent.com"):
        raise ValueError("Unexpected artifact download host")
    print("::add-mask::" + url, flush=True)
    folder = Path("release-files"); folder.mkdir(exist_ok=True)
    wrapper = folder / "artifact.zip"
    with urllib.request.urlopen(url, timeout=90) as response, wrapper.open("wb") as target:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 1_500_000_000: raise ValueError("Artifact too large")
            target.write(chunk)
    allowed = {"WoR-Advisor-windows-x64.zip", "WoR-Advisor-windows-x64.zip.sha256", "source-commit.txt"}
    with zipfile.ZipFile(wrapper) as archive:
        if set(archive.namelist()) != allowed: raise ValueError("Unexpected artifact contents")
        archive.extractall(folder)
    wrapper.unlink()
    if (folder / "source-commit.txt").read_text(encoding="utf-8-sig").strip() != commit: raise ValueError("Source commit mismatch")
    portable = folder / "WoR-Advisor-windows-x64.zip"
    with portable.open("rb") as binary:digest = hashlib.file_digest(binary, "sha256").hexdigest()
    checksum = (folder / "WoR-Advisor-windows-x64.zip.sha256").read_text(encoding="utf-8-sig").split()[0]
    if digest != expected or digest != checksum: raise ValueError("Portable ZIP checksum mismatch")
    with zipfile.ZipFile(portable) as archive:
        if archive.testzip() is not None: raise ValueError("Corrupt ZIP")
        names = [name.replace("\\", "/") for name in archive.namelist()]
        if not any(name.endswith("/WoR-Advisor.exe") for name in names): raise ValueError("Windows executable missing")
        if any("/test/fixtures/" in name or name.lower().endswith((".sqlite3", ".sqlite", ".db")) for name in names): raise ValueError("User or test data present in release")
        database = next(name for name in archive.namelist() if name.endswith("vendor/wor-scanner/data/db.json"))
        catalog = json.loads(archive.read(database))
        if set(catalog) != {"version", "heroes"} or any("owned" in h or "price" in h for h in catalog["heroes"]): raise ValueError("Scanner data is not a clean catalog")
    filename = "WoR-Advisor-" + version + "-windows-x64.zip"
    target = folder / filename; portable.replace(target)
    sha = folder / (filename + ".sha256"); sha.write_text(expected + "  " + filename + "\n", encoding="ascii")
    notes = Path("docs/release-notes-template.md").read_text(encoding="utf-8")
    notes += f"\n\nВерсия: {version}\nПроверенная сборка: https://github.com/Jlapb-Main/wor-advisor/actions/runs/{run}\nИсходный коммит: `{commit}`\nSHA-256: `{expected}`\n"
    body = folder / "notes.md"; body.write_text(notes, encoding="utf-8")
    if current.returncode != 0:
        gh("release", "create", tag, "--repo", repository, "--draft", "--target", os.environ["GITHUB_SHA"], "--title", "WoR Advisor " + version + " — Windows x64", "--notes-file", str(body))
    else:
        gh("release", "edit", tag, "--repo", repository, "--draft", "--title", "WoR Advisor " + version + " — Windows x64", "--notes-file", str(body))
    gh("release", "upload", tag, str(target), str(sha), "--repo", repository, "--clobber")
    gh("release", "edit", tag, "--repo", repository, "--draft=false", "--latest")
    release = json.loads(gh("release", "view", tag, "--repo", repository, "--json", "url,assets,isDraft", capture=True))
    if release["isDraft"]: raise ValueError("Release is still a draft")
    asset = next(asset for asset in release["assets"] if asset["name"] == filename)
    if asset["size"] != target.stat().st_size: raise ValueError("Uploaded asset size mismatch")
    channel = {"schema_version": 1, "product": "WoR Advisor", "channel": "stable", "release": {
        "version": version, "download_url": asset["url"], "sha256": expected,
        "source_commit": commit, "notes": release["url"], "size_bytes": target.stat().st_size,
        "released_at": datetime.now(timezone.utc).isoformat()}}
    if not asset["url"].startswith("https://github.com/Jlapb-Main/wor-advisor-updates/releases/download/"):
        raise ValueError("Unexpected public download URL")
    Path("channels/stable.json").write_text(json.dumps(channel, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "channels/stable.json"], check=True)
    subprocess.run(["git", "commit", "-m", "Publish stable channel " + version], check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
    print("Published " + release["url"], flush=True)


if __name__ == "__main__": main()
