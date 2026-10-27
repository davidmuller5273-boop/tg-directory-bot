import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "deploy" / "ubuntu" / "backup-to-github.sh"
NEW = ROOT / "deploy" / "ubuntu" / "new-server-restore.sh"
HAVE_TOOLS = all(shutil.which(t) for t in ("bash", "git", "openssl", "tar", "flock"))
PASSWORD = "My$trong 'pass' 密码 #1"
SECRET = "BOT_TOKEN=123456:SECRET-TOKEN-VALUE"


def make_db(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (marker,))
    con.commit()
    con.close()


def write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@unittest.skipUnless(HAVE_TOOLS, "needs bash/git/openssl/tar/flock")
class NewServerRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        t = self.tmp
        self.old_app = t / "old-app"
        self.old_state = t / "old-state"
        self.bare = t / "backup.git"
        self.new_app = t / "new-app"
        self.state = t / "new-state"
        self.restore = t / "tg-restore"
        self.restore_hooks = t / "restore.d"
        self.log = t / "calls.log"
        self.old_state.mkdir()
        (self.old_state / "backup.pass").write_text(PASSWORD)
        make_db(self.old_app / "data" / "directory.sqlite3", "v1")
        (self.old_app / ".env").write_text(SECRET + "\nDB_PATH=data/directory.sqlite3\n")
        (self.old_app / ".env").chmod(0o600)
        self.old_hooks = t / "old-hooks"
        write_exec(self.old_hooks / "safe.sh", 'echo "SAFE_DUMP" > "$1/safe/safe.sql"; echo "SAFE=1" > "$1/safe/.env"; chmod 600 "$1/safe/.env"\n')
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        # fake public code repo
        code = t / "code-src"
        write_exec(code / "deploy" / "ubuntu" / "install.sh",
                   f'#!/usr/bin/env bash\necho "install APP_DIR=$APP_DIR env_mode=$(stat -c %a "$APP_DIR/.env")" >> "{self.log}"\n')
        write_exec(code / "deploy" / "ubuntu" / "setup-backup.sh",
                   f'#!/usr/bin/env bash\necho "setup passlen=${{#BACKUP_PASS}}" >> "{self.log}"\necho "部署公钥: ssh-ed25519 AAAAFAKEWRITE"\n')
        (code / "run.py").write_text("print('bot')\n")
        (code / "requirements.txt").write_text("")
        subprocess.run(["git", "init", "-q", "-b", "main", str(code)], check=True)
        subprocess.run(["git", "-C", str(code), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(code), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
        self.code = code
        # stubs
        self.bin = t / "bin"
        write_exec(self.bin / "ssh-keygen",
                   '#!/usr/bin/env bash\nwhile [[ $# -gt 0 ]]; do [[ "$1" == "-f" ]] && f="$2"; shift; done\n'
                   'echo PRIVATE > "$f"; echo "ssh-ed25519 AAAAFAKEKEY fake@test" > "$f.pub"\n')
        self.systemctl = t / "systemctl"
        write_exec(self.systemctl, f'#!/usr/bin/env bash\necho "systemctl $*" >> "{self.log}"\nexit 0\n')

    def backup(self, **extra):
        env = dict(os.environ, APP_DIR=str(self.old_app), STATE_DIR=str(self.old_state), WORK_DIR=str(self.tmp / "old-work"),
                   BACKUP_REPO_URL=str(self.bare), BACKUP_HOOKS_DIR=str(self.old_hooks))
        env.update(extra)
        res = subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def run_new(self, password=PASSWORD, **extra):
        env = {k: v for k, v in os.environ.items() if k != "BACKUP_PASS"}
        env.update(
            NEW_SERVER_TEST="1", APP_DIR=str(self.new_app), STATE_DIR=str(self.state), RESTORE_DIR=str(self.restore),
            BACKUP_REPO_URL=str(self.bare), REPO_URL=str(self.code), RESTORE_HOOKS_DIR=str(self.restore_hooks),
            SYSTEMCTL=str(self.systemctl), SERVICE_WAIT="0", SAFE_REPO_HTTPS=str(self.tmp / "no-such-safe.git"),
            PATH=f"{self.bin}:{os.environ['PATH']}",
        )
        if password is not None:
            env["BACKUP_PASS"] = password
        env.update(extra)
        return subprocess.run(["bash", str(NEW)], env=env, capture_output=True, text=True)

    def assertNoPassword(self, res):
        self.assertNotIn(PASSWORD, res.stdout + res.stderr)
        self.assertNotIn("SECRET-TOKEN-VALUE", res.stdout + res.stderr)

    def test_full_restore_picks_newest_and_dispatches_hooks(self):
        import time
        self.backup()
        time.sleep(1.1)
        con = sqlite3.connect(self.old_app / "data" / "directory.sqlite3")
        con.execute("INSERT INTO t VALUES ('v2')")
        con.commit()
        con.close()
        self.backup()
        write_exec(self.restore_hooks / "safe.sh",
                   f'#!/usr/bin/env bash\necho "hook arg=$1 part=$RESTORE_PART_DIR dump=$(cat "$1/safe/safe.sql") envmode=$(stat -c %a "$1/safe/.env")" >> "{self.log}"\n')
        res = self.run_new()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNoPassword(res)
        self.assertIn("可以访问备份仓库", res.stdout)
        # restored files, owner modes
        con = sqlite3.connect(self.new_app / "data" / "directory.sqlite3")
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        con.close()
        env = self.new_app / ".env"
        self.assertEqual(env.read_text(), (self.old_app / ".env").read_text())
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertTrue((self.new_app / "run.py").exists())
        self.assertFalse((self.new_app / ".git").exists())
        # newest backup picked
        con = sqlite3.connect(self.new_app / "data" / "directory.sqlite3")
        self.assertEqual([r[0] for r in con.execute("SELECT v FROM t ORDER BY rowid")], ["v1", "v2"])  # the newest backup
        con.close()
        marker = (self.state / "restored-from").read_text()
        out = subprocess.run(["git", "--git-dir", str(self.bare), "ls-tree", "--name-only", "main"], capture_output=True, text=True).stdout.split()
        files = sorted(f for f in out if f.endswith(".tar.gz.enc"))
        self.assertEqual(len(files), 2)
        self.assertEqual(marker, files[-1])
        log = self.log.read_text()
        self.assertIn("install APP_DIR=", log)
        self.assertIn("env_mode=600", log)
        self.assertIn("systemctl restart tg-directory-bot tg-directory-bot-web", log)
        self.assertIn(f"setup passlen={len(PASSWORD)}", log)
        self.assertIn(f"hook arg={self.restore}", log)
        self.assertIn("dump=SAFE_DUMP", log)
        self.assertIn("envmode=600", log)
        self.assertIn("部署公钥", res.stdout)
        self.assertIn("188.ms", res.stdout)
        self.assertIn(f"sudo rm -rf {self.restore}", res.stdout)

    def test_works_when_piped_into_bash(self):
        # curl ... | sudo bash feeds the script on stdin: child commands must not swallow it
        self.backup()
        env = {k: v for k, v in os.environ.items() if k != "BACKUP_PASS"}
        env.update(NEW_SERVER_TEST="1", APP_DIR=str(self.new_app), STATE_DIR=str(self.state), RESTORE_DIR=str(self.restore),
                   BACKUP_REPO_URL=str(self.bare), REPO_URL=str(self.code), RESTORE_HOOKS_DIR=str(self.restore_hooks),
                   SYSTEMCTL=str(self.systemctl), SERVICE_WAIT="0", SAFE_REPO_HTTPS=str(self.tmp / "none.git"),
                   PATH=f"{self.bin}:{os.environ['PATH']}", BACKUP_PASS=PASSWORD)
        res = subprocess.run(["bash"], input=NEW.read_text(), env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("恢复完成", res.stdout)
        self.assertTrue((self.new_app / ".env").exists())

    def test_key_not_registered_yet(self):
        res = self.run_new(BACKUP_REPO_URL=str(self.tmp / "missing.git"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("公钥已生成，添加后重新运行同一条命令", res.stdout)
        self.assertIn("ssh-ed25519 AAAAFAKEKEY fake@test", res.stdout)
        self.assertTrue((self.state / "restore_ed25519").exists())
        self.assertFalse(self.new_app.exists())
        self.assertNoPassword(res)
        # re-run reuses the same key
        res2 = self.run_new(BACKUP_REPO_URL=str(self.tmp / "missing.git"))
        self.assertIn("AAAAFAKEKEY", res2.stdout)

    def test_wrong_password(self):
        self.backup()
        res = self.run_new(password="wrong-password-123")
        self.assertEqual(res.returncode, 1)
        self.assertIn("解密失败", res.stderr)
        self.assertFalse(self.new_app.exists())
        self.assertFalse(self.restore.exists())
        self.assertNotIn("wrong-password-123", res.stdout + res.stderr)

    def test_missing_password(self):
        res = self.run_new(password=None)
        self.assertEqual(res.returncode, 1)
        self.assertIn("BACKUP_PASS", res.stderr)

    def test_no_backups_in_repo(self):
        res = self.run_new()
        self.assertEqual(res.returncode, 1)
        self.assertIn("没有任何备份文件", res.stderr)

    def test_rerun_skips_and_existing_data_protected(self):
        self.backup()
        self.assertEqual(self.run_new().returncode, 0)
        (self.new_app / "data" / "directory.sqlite3").write_bytes(b"")  # simulate later changes
        res = self.run_new()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("已经恢复过", res.stdout)
        self.assertEqual((self.new_app / "data" / "directory.sqlite3").read_bytes(), b"")  # not overwritten
        # a server that already has data but no marker is protected
        (self.state / "restored-from").unlink()
        res = self.run_new()
        self.assertEqual(res.returncode, 1)
        self.assertIn("FORCE_RESTORE=1", res.stderr)
        res = self.run_new(FORCE_RESTORE="1")
        self.assertEqual(res.returncode, 0, res.stderr)
        con = sqlite3.connect(self.new_app / "data" / "directory.sqlite3")
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        con.close()

    def test_failing_restore_hook_reported(self):
        self.backup()
        write_exec(self.restore_hooks / "safe.sh", "#!/usr/bin/env bash\nexit 5\n")
        res = self.run_new()
        self.assertEqual(res.returncode, 1)
        self.assertIn("恢复钩子 safe", res.stdout + res.stderr)
        self.assertTrue((self.new_app / ".env").exists())  # rest still restored

    def test_safe_without_hook_private_and_public(self):
        self.backup()
        res = self.run_new()  # no hook installed, safe repo unreachable -> deploy key idea
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("safe 仓库是私有的", res.stdout)
        self.assertIn("safe_ed25519", res.stdout)
        self.assertIn(f"BACKUP_DECRYPTED={self.restore} bash safe-restore/deploy/ubuntu/restore-from-backup.sh", res.stdout)
        self.assertIn("ssh-ed25519 AAAAFAKEKEY", res.stdout)
        # public safe repo -> plain one-liner, no key needed
        pub = self.tmp / "safe-pub.git"
        subprocess.run(["git", "init", "-q", "--bare", str(pub)], check=True)
        (self.state / "restored-from").unlink()
        res = self.run_new(SAFE_REPO_HTTPS=str(pub), FORCE_RESTORE="1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("safe 仓库是私有的", res.stdout)
        self.assertIn(f"git clone {pub} safe-restore && sudo BACKUP_DECRYPTED={self.restore} bash", res.stdout)

    @unittest.skipUnless(shutil.which("curl"), "needs curl")
    def test_safe_restore_url_runs(self):
        self.backup()
        script = self.tmp / "safe-restore.sh"
        write_exec(script, f'#!/usr/bin/env bash\necho "saferun dec=$BACKUP_DECRYPTED" >> "{self.log}"\n')
        res = self.run_new(SAFE_RESTORE_URL=f"file://{script}")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(f"saferun dec={self.restore}", self.log.read_text())
        self.assertNotIn("safe 仓库是私有的", res.stdout)


class StaticTests(unittest.TestCase):
    def test_parses_and_contract(self):
        subprocess.run(["bash", "-n", str(NEW)], check=True)
        text = NEW.read_text()
        for needle in ("公钥已生成，添加后重新运行同一条命令", "/etc/tg-bot-backup.restore.d", "-pass env:BACKUP_PASS",
                       "exec </dev/null", "BACKUP_DECRYPTED", "188.ms", "AI_API_KEY"):
            self.assertIn(needle, text)
        self.assertNotRegex(text, r"echo[^\n]*\$BACKUP_PASS")
        self.assertNotRegex(text, r"-pass pass:")


if __name__ == "__main__":
    unittest.main()
