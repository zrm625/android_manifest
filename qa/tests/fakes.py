"""Host-side fakes for the phone. Nothing here runs adb or fastboot."""
import re


class FakeShell:
    """A Device stand-in for shell reads. ``replies`` maps a regex (searched in
    the command) to (rc, stdout) or to a callable(cmd) -> (rc, stdout). The
    first match wins; unmatched commands return (0, "")."""

    serial = "ZY225DPT7F"

    def __init__(self, replies=None, bytes_replies=None, boot_id="B"):
        self.replies = list((replies or {}).items())
        self.bytes_replies = list((bytes_replies or {}).items())
        self._boot_id = boot_id
        self.cmds = []

    def _match(self, table, cmd, default):
        for pat, rep in table:
            if re.search(pat, cmd):
                return rep(cmd) if callable(rep) else rep
        return default

    def root(self, seconds=60):
        self.cmds.append("<root>")

    def sh_rc(self, cmd, timeout=30):
        self.cmds.append(cmd)
        rc, out = self._match(self.replies, cmd, (0, ""))
        return rc, out, "" if rc == 0 else "error: fake failure\n"

    def sh(self, cmd, timeout=30):
        return self.sh_rc(cmd, timeout)[1]

    def read(self, cmd, timeout=30):
        from riverqa.device import Read
        rc, out, err = self.sh_rc(cmd, timeout)
        return Read(rc, out, err)

    def read_files(self, glob, timeout=60):
        """Device.read_files through the real parser. ``bytes_replies`` maps a
        regex (searched in the glob) to (rc, files): files is bytes (one file,
        none if empty), a list of (path, bytes), or str (the raw remote
        output, to simulate a cut-off or corrupt transfer)."""
        import base64
        from riverqa.device import b64_script, parse_b64_dump
        self.cmds.append(b64_script(glob))
        rc, files = self._match(self.bytes_replies, glob, (0, []))
        if isinstance(files, str):
            return parse_b64_dump(rc, files)
        if isinstance(files, bytes):
            files = [(glob.replace("*", "0"), files)] if files else []
        if rc == 0 and not files:
            return parse_b64_dump(7, "")       # the real script: the glob matched nothing
        text = "".join(f"__FILE__ {len(b)} {p}\n" + base64.encodebytes(b).decode() + "__EOF__\n"
                       for p, b in files) + f"__RC__ {len(files)}\n"
        return parse_b64_dump(rc, text)

    def boot_id(self):
        return self._boot_id

    def state(self):
        return "device"

    def in_fastboot(self):
        return False

    def getprop(self, name):
        return self.sh(f"getprop {name}").strip()


DROPBOX_EMPTY = "Drop box contents: 0 entries\nMax entries: 1000\n"
