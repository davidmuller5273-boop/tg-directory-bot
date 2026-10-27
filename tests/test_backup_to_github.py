import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "deploy" / "ubuntu" / "backup-to-github.sh"
RESTORE = ROOT / "deploy" / "ubuntu" / "restore-backup.sh"
SETUP = ROOT / "deploy" / "ubuntu" / "setup-backup.sh"
WRAPPER = ROOT / "deploy" / "ubuntu" / "tgbot-backup.sh"
HAVE_TOOLS = all(shutil.which(t) for t in ("bash", "git", "openssl", "tar", "flock"))


def make_db(path: Path, rows: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    con.commit()
    con.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "app"
        self.state = self.tmp / "state"
        self.work = self.tmp / "work"
        self.bare = self.tmp / "remote.git"
        self.state.mkdir()
        (self.state / "backup.pass").write_text("test-passphrase-xxxx")
        make_db(self.app / "data" / "directory.sqlite3")
        make_db(self.app / "data" / "clones" / "clone-1.sqlite3", 2)
        make_db(self.app / "data" / "clones" / "deleted" / "clone-9.sqlite3.old", 1)
        (self.app / ".env").write_text("BOT_TOKEN=123456:SECRET-TOKEN-VALUE\nDB_PATH=data/directory.sqlite3\n")
        (self.app / "backups").mkdir()
        (self.app / "backups" / "old.sqlite3").write_bytes(b"x")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)

    def env(self, **extra):
        env = dict(os.environ)
        env.update(
            APP_DIR=str(self.app), STATE_DIR=str(self.state), WORK_DIR=str(self.work),
            BACKUP_REPO_URL=str(self.bare),
        )
        env.update(extra)
        return env

    def run_backup(self, **extra):
        return subprocess.run(["bash", str(BACKUP)], env=self.env(**extra), capture_output=True, text=True)

    def remote_files(self):
        out = subprocess.run(["git", "--git-dir", str(self.bare), "ls-tree", "--name-only", "main"],
                             capture_output=True, text=True, check=True).stdout.split()
        return sorted(out)

    def remote_commits(self):
        return int(subprocess.run(["git", "--git-dir", str(self.bare), "rev-list", "--count", "main"],
                                  capture_output=True, text=True, check=True).stdout)

    def backup_files(self):
        return [f for f in self.remote_files() if f.endswith(".tar.gz.enc")]


@unittest.skipUnless(HAVE_TOOLS, "needs bash/git/openssl/tar/flock")
class BackupToGithubTests(_Base):
    def test_backup_encrypt_restore_roundtrip(self):
        # a writer holding the DB open (WAL) must not break the online backup
        live = sqlite3.connect(self.app / "data" / "directory.sqlite3")
        live.execute("INSERT INTO t (v) VALUES ('uncheckpointed')")
        live.commit()
        result = self.run_backup()
        live.close()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        files = self.backup_files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^tg-bot-backup-\d{8}-\d{6}\.tar\.gz\.enc$")
        # repo only holds encrypted blobs (+README), no plaintext db / env
        self.assertEqual(sorted(set(self.remote_files()) - set(files)), ["README.md"])

        checkout = self.tmp / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(checkout)], check=True)
        blob = checkout / files[0]
        raw = blob.read_bytes()
        self.assertTrue(raw.startswith(b"Salted__"))
        self.assertNotIn(b"SQLite format", raw)
        self.assertNotIn(b"SECRET-TOKEN-VALUE", raw)

        out = self.tmp / "restored"
        res = subprocess.run(["bash", str(RESTORE), str(blob), str(out)],
                             env=self.env(PASS_FILE=str(self.state / "backup.pass")), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("directory.sqlite3: ok", res.stdout)
        self.assertIn("clone-1.sqlite3: ok", res.stdout)
        con = sqlite3.connect(out / "data" / "directory.sqlite3")
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(con.execute("SELECT count(*) FROM t").fetchone()[0], 6)
        con.close()
        # not backed up: .env, backups/, deleted archive
        names = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
        self.assertNotIn(".env", names)
        self.assertFalse(any(n.startswith("backups/") for n in names))
        self.assertFalse(any("deleted" in n for n in names))
        self.assertIn("MANIFEST.txt", names)
        self.assertNotIn("SECRET-TOKEN-VALUE", (out / "MANIFEST.txt").read_text())

    def test_wrong_passphrase_cannot_decrypt(self):
        self.assertEqual(self.run_backup().returncode, 0)
        checkout = self.tmp / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(checkout)], check=True)
        wrong = self.tmp / "wrong.pass"
        wrong.write_text("nope")
        res = subprocess.run(["bash", str(RESTORE), str(checkout / self.backup_files()[0]), str(self.tmp / "o")],
                             env=self.env(PASS_FILE=str(wrong)), capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)

    def test_retention_keeps_only_newest_three(self):
        names = []
        for stamp in ("a", "b", "c", "d"):
            result = self.run_backup()
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            names.append(self.backup_files())
            # make file names differ: stamps have 1s resolution
            time.sleep(1.1)
        self.assertEqual([len(n) for n in names], [1, 2, 3, 3])
        self.assertNotIn(names[0][0], names[3])  # oldest dropped
        self.assertEqual(names[3][1:], sorted(names[3])[1:])

    def test_backup_keep_variable_and_history_squash(self):
        for _ in range(4):
            result = self.run_backup(BACKUP_KEEP="1", BACKUP_MAX_COMMITS="3")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            time.sleep(1.1)
        self.assertEqual(len(self.backup_files()), 1)
        # 4 runs, limit 3 -> squashed at the 4th run into a single commit
        self.assertEqual(self.remote_commits(), 1)
        # a following run still works on top of the squashed history
        self.assertEqual(self.run_backup(BACKUP_KEEP="1", BACKUP_MAX_COMMITS="3").returncode, 0)
        self.assertEqual(self.remote_commits(), 2)
        self.assertEqual(len(self.backup_files()), 1)

    def test_invalid_keep_rejected(self):
        self.assertNotEqual(self.run_backup(BACKUP_KEEP="0").returncode, 0)

    def test_unreachable_remote_exits_3(self):
        result = self.run_backup(BACKUP_REPO_URL=str(self.tmp / "missing.git"))
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_missing_passphrase_fails_without_pushing(self):
        (self.state / "backup.pass").unlink()
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        refs = subprocess.run(["git", "--git-dir", str(self.bare), "for-each-ref"], capture_output=True, text=True).stdout
        self.assertEqual(refs.strip(), "")

    def test_extra_env_db_path_outside_data_is_included(self):
        extra = self.tmp / "elsewhere" / "main.sqlite3"
        make_db(extra, 3)
        (self.app / ".env").write_text(f"DB_PATH={extra}\n")
        self.assertEqual(self.run_backup().returncode, 0)
        checkout = self.tmp / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(checkout)], check=True)
        out = self.tmp / "restored"
        res = subprocess.run(["bash", str(RESTORE), str(checkout / self.backup_files()[0]), str(out)],
                             env=self.env(PASS_FILE=str(self.state / "backup.pass")), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((out / "data" / "extra" / "main.sqlite3").exists())


def pass_segment() -> str:
    text = SETUP.read_text()
    start = text.index("PASS_MODE=keep")
    end = text.index('chmod 600 "$STATE_DIR/backup.pass"')
    return text[start:end]


class SetupPasswordTests(unittest.TestCase):
    def run_segment(self, state: Path, **env):
        script = "set -Eeuo pipefail\n" + pass_segment() + 'printf "%s" "$PASS_MODE"\n'
        full = {k: v for k, v in os.environ.items() if k != "BACKUP_PASS"}
        full.update(env, STATE_DIR=str(state))
        return subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True)

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)

    def test_custom_password_written_verbatim(self):
        weird = "p@ss $(touch /tmp/pwned-xx) `id` 'q' \"d\" \\ ;&|*密码"
        res = self.run_segment(self.state, BACKUP_PASS=weird)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "custom")
        self.assertEqual((self.state / "backup.pass").read_text(), weird)
        self.assertNotIn("pwned", res.stdout + res.stderr)
        self.assertFalse(Path("/tmp/pwned-xx").exists())
        self.assertEqual(oct((self.state / "backup.pass").stat().st_mode & 0o777), "0o600")

    def test_short_or_empty_rejected(self):
        for bad in ("", "short", "1234567", "密码太短了"):
            res = self.run_segment(self.state, BACKUP_PASS=bad)
            self.assertEqual(res.returncode, 1, bad)
            self.assertIn("8 个字符", res.stderr)
            self.assertFalse((self.state / "backup.pass").exists())
            self.assertNotIn(bad or "\0", res.stdout)

    def test_random_when_not_given_and_kept_on_rerun(self):
        res = self.run_segment(self.state)
        self.assertEqual(res.stdout, "random")
        first = (self.state / "backup.pass").read_text()
        self.assertGreaterEqual(len(first), 32)
        res = self.run_segment(self.state)
        self.assertEqual(res.stdout, "keep")
        self.assertEqual((self.state / "backup.pass").read_text(), first)

    def test_changed_password_reported(self):
        self.run_segment(self.state, BACKUP_PASS="first-password-1")
        res = self.run_segment(self.state, BACKUP_PASS="second-password-2")
        self.assertEqual(res.stdout, "changed")
        self.assertEqual((self.state / "backup.pass").read_text(), "second-password-2")
        res = self.run_segment(self.state, BACKUP_PASS="second-password-2")
        self.assertEqual(res.stdout, "custom")

    def test_custom_password_roundtrip_with_backup(self):
        # a password set this way must work with openssl -pass file:
        self.run_segment(self.state, BACKUP_PASS="my own pass 密码!")
        enc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", f"file:{self.state/'backup.pass'}"],
            input=b"hello", capture_output=True, check=True).stdout
        dec = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "pass:my own pass 密码!"],
            input=enc, capture_output=True, check=True).stdout
        self.assertEqual(dec, b"hello")


@unittest.skipUnless(HAVE_TOOLS, "needs bash/git/openssl/tar/flock")
class HooksAndWrapperTests(_Base):
    def setUp(self):
        super().setUp()
        self.hooks = self.tmp / "hooks.d"
        self.hooks.mkdir()

    def env(self, **extra):
        return super().env(BACKUP_HOOKS_DIR=str(self.hooks), **extra)

    def hook(self, name, body, mode=0o755):
        path = self.hooks / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(mode)

    def restored(self):
        checkout = self.tmp / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(checkout)], check=True)
        out = self.tmp / "restored"
        res = subprocess.run(["bash", str(RESTORE), str(checkout / self.backup_files()[0]), str(out)],
                             env=self.env(PASS_FILE=str(self.state / "backup.pass")), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        return out

    def test_hook_output_included(self):
        self.hook("safe.sh", 'echo "CREATE TABLE x;" > "$1/safe/dump.sql"\n')
        res = self.run_backup()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        out = self.restored()
        self.assertEqual((out / "safe" / "dump.sql").read_text().strip(), "CREATE TABLE x;")
        self.assertIn("hooks: safe", (out / "MANIFEST.txt").read_text())
        self.assertTrue((out / "data" / "directory.sqlite3").exists())

    def test_failing_hook_reported_but_others_continue(self):
        self.hook("a-bad.sh", 'echo partial > "$1/a-bad/x.sql"; exit 7\n')
        self.hook("b-good.sh", 'echo ok > "$1/b-good/ok.txt"\n')
        self.hook("c-notexec.sh", 'echo no > "$1/c-notexec/n.txt"\n', mode=0o644)
        res = self.run_backup()
        self.assertEqual(res.returncode, 4, res.stderr + res.stdout)
        self.assertIn("a-bad", res.stdout)
        out = self.restored()
        self.assertFalse((out / "a-bad").exists())
        self.assertFalse((out / "c-notexec").exists())
        self.assertEqual((out / "b-good" / "ok.txt").read_text().strip(), "ok")
        self.assertTrue((out / "data" / "directory.sqlite3").exists())

    def test_env_files_from_hooks_are_stripped(self):
        self.hook("safe.sh", 'echo TOKEN=SECRET-HOOK-VALUE > "$1/safe/.env"; echo TOKEN=SECRET-HOOK-VALUE > "$1/safe/prod.env"; echo ok > "$1/safe/keep.txt"\n')
        self.assertEqual(self.run_backup().returncode, 0)
        out = self.restored()
        self.assertTrue((out / "safe" / "keep.txt").exists())
        names = {p.name for p in out.rglob("*") if p.is_file()}
        self.assertNotIn(".env", names)
        self.assertNotIn("prod.env", names)

    def test_corrupt_database_does_not_stop_hooks_or_other_dbs(self):
        (self.app / "data" / "clones" / "clone-1.sqlite3").write_bytes(b"not a database" * 100)
        self.hook("safe.sh", 'echo ok > "$1/safe/ok.txt"\n')
        res = self.run_backup()
        self.assertEqual(res.returncode, 4, res.stderr + res.stdout)
        out = self.restored()
        self.assertTrue((out / "data" / "directory.sqlite3").exists())
        self.assertFalse((out / "data" / "clones" / "clone-1.sqlite3").exists())
        self.assertTrue((out / "safe" / "ok.txt").exists())

    def wrapper(self, *args):
        lib = self.tmp / "lib"
        lib.mkdir(exist_ok=True)
        shutil.copy(BACKUP, lib / "backup-to-github.sh")
        return subprocess.run(["bash", str(WRAPPER), *args], capture_output=True, text=True,
                              env=self.env(LIB_DIR=str(lib), TGBOT_BACKUP_NO_SUDO="1"))

    def test_tgbot_backup_run_and_list(self):
        res = self.wrapper("list")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("还没有做过备份", res.stdout)
        for _ in range(4):
            res = self.wrapper()
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("备份完成", res.stdout)
            time.sleep(1.1)
        res = self.wrapper("list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("共 3 份", res.stdout)
        self.assertEqual(res.stdout.count(".tar.gz.enc"), 3)
        newest = sorted(self.backup_files())[-1]
        self.assertLess(res.stdout.index(newest), res.stdout.index(sorted(self.backup_files())[0]))

    def test_tgbot_backup_reports_push_failure(self):
        lib = self.tmp / "lib"
        lib.mkdir()
        shutil.copy(BACKUP, lib / "backup-to-github.sh")
        res = subprocess.run(["bash", str(WRAPPER)], capture_output=True, text=True,
                             env=self.env(LIB_DIR=str(lib), TGBOT_BACKUP_NO_SUDO="1",
                                          BACKUP_REPO_URL=str(self.tmp / "missing.git")))
        self.assertEqual(res.returncode, 3)
        self.assertIn("推送到 GitHub 失败", res.stdout)


class ScriptStaticTests(unittest.TestCase):
    def test_scripts_parse(self):
        for script in (BACKUP, RESTORE, SETUP, WRAPPER):
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_setup_contents(self):
        text = SETUP.read_text()
        self.assertIn("/etc/tg-bot-backup", text)
        self.assertIn("sudo cat $STATE_DIR/backup.pass", text)
        self.assertIn("printf '%s' \"$BACKUP_PASS\"", text)
        self.assertIn("旧密码", text)
        self.assertIn("公钥还没添加，等通知后再运行 sudo systemctl start tg-bot-backup", text)
        self.assertIn("BACKUP_KEEP:-3", text)
        self.assertIn("/usr/local/bin/tgbot-backup", text)
        self.assertIn("/etc/tg-bot-backup.d", text)
        self.assertIn("Persistent=true", text)
        self.assertNotRegex(text, r"echo[^\n]*\$BACKUP_PASS")
        self.assertIn('"${BACKUP_KEEP:-3}"', BACKUP.read_text())

    def test_no_secrets_in_scripts(self):
        for script in (BACKUP, RESTORE, SETUP, WRAPPER):
            text = script.read_text()
            self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", text)
            self.assertNotIn("ghp_", text)


if __name__ == "__main__":
    unittest.main()
