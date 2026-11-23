"""Build the river-qa helper APK offline: aapt2, javac, d8, zipalign and
apksigner from build-tools 37 against platforms/android-36/android.jar,
signed with the debug key. The cache key covers the sources, the toolchain
versions and forge's LAN address (it is baked into the cleartext allowlist)."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from .core import QA_DIR, bench_path, read_json, run

SDK = Path(os.environ.get("ANDROID_SDK_ROOT", Path.home() / ".local/share/mise/installs/android-sdk/23.0"))
BUILD_TOOLS = SDK / "build-tools" / "37.0.0"
ANDROID_JAR = SDK / "platforms" / "android-36" / "android.jar"
KEYSTORE = Path(os.environ.get("RIVERQA_KEYSTORE", Path.home() / ".config/.android/debug.keystore"))
HELPER = QA_DIR / "helper"
PACKAGE = "river.qa"


def server_config() -> dict:
    return (read_json(QA_DIR / "fixtures.json", {}) or {}).get("server", {})


def _javac() -> str:
    return shutil.which("javac") or "javac"


def cache_key() -> str:
    h = hashlib.sha256()
    for p in sorted(HELPER.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(HELPER)).encode())
            h.update(p.read_bytes())
    h.update(str(BUILD_TOOLS).encode())
    h.update(hashlib.sha256(ANDROID_JAR.read_bytes()).digest())
    jv = run([_javac(), "-version"], timeout=30)
    h.update((jv.stdout + jv.stderr).encode())
    h.update(server_config().get("lan_addr", "").encode())
    h.update(KEYSTORE.read_bytes())
    return h.hexdigest()


def ensure_helper(verbose: bool = False) -> tuple[Path, str]:
    key = cache_key()
    out_dir = bench_path("qa", "helper")
    apk = out_dir / f"river-qa-{key[:16]}.apk"
    if apk.exists():
        return apk, key
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="river-qa-build-") as tmp:
        t = Path(tmp)
        _build(t, apk, verbose)
    return apk, key


def _ok(r, what):
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {(r.stderr or r.stdout)[-1500:]}")


def _build(t: Path, apk: Path, verbose: bool) -> None:
    bt = BUILD_TOOLS
    lan = server_config().get("lan_addr", "")
    if not lan:
        raise RuntimeError("qa/fixtures.json has no server.lan_addr")
    res = t / "res"
    shutil.copytree(HELPER / "res", res)
    tmpl = res / "xml" / "network_security_config.xml.in"
    (res / "xml" / "network_security_config.xml").write_text(tmpl.read_text().replace("@LAN_ADDR@", lan))
    tmpl.unlink()
    flat = t / "res.zip"
    _ok(run([str(bt / "aapt2"), "compile", "--dir", str(res), "-o", str(flat)], timeout=120), "aapt2 compile")
    gen = t / "gen"
    gen.mkdir()
    base = t / "base.apk"
    _ok(run([str(bt / "aapt2"), "link", "-I", str(ANDROID_JAR), "--manifest", str(HELPER / "AndroidManifest.xml"),
             "--java", str(gen), "-o", str(base), str(flat)], timeout=120), "aapt2 link")
    classes = t / "classes"
    classes.mkdir()
    srcs = [str(p) for p in sorted((HELPER / "src").rglob("*.java"))] + [str(p) for p in gen.rglob("*.java")]
    # --release 17 takes java.* from the JDK's API (android.jar's stubs lack
    # LambdaMetafactory.metafactory); d8 desugars lambdas for Android.
    _ok(run([_javac(), "--release", "17", "-encoding", "UTF-8", "-nowarn",
             "-classpath", str(ANDROID_JAR), "-d", str(classes), *srcs], timeout=300), "javac")
    dex = t / "dex"
    dex.mkdir()
    class_files = [str(p) for p in classes.rglob("*.class")]
    _ok(run([str(bt / "d8"), "--min-api", "35", "--lib", str(ANDROID_JAR), "--output", str(dex), *class_files],
            timeout=300), "d8")
    with zipfile.ZipFile(base, "a", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(dex / "classes.dex", "classes.dex")
    aligned = t / "aligned.apk"
    _ok(run([str(bt / "zipalign"), "-f", "-p", "4", str(base), str(aligned)], timeout=120), "zipalign")
    signed = t / "signed.apk"
    _ok(run([str(bt / "apksigner"), "sign", "--ks", str(KEYSTORE), "--ks-pass", "pass:android",
             "--ks-key-alias", "androiddebugkey", "--key-pass", "pass:android", "--out", str(signed), str(aligned)],
            timeout=120), "apksigner")
    _ok(run([str(bt / "apksigner"), "verify", str(signed)], timeout=60), "apksigner verify")
    shutil.copyfile(signed, apk)
    if verbose:
        print(f"built {apk}")
