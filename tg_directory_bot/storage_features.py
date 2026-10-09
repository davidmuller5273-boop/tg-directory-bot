"""Storage for 2026-10 features: activity tiers, invite message/boost rewards,
raffle draw history helpers. Mixed into DirectoryStore."""
from __future__ import annotations

import sqlite3
from decimal import Decimal


def _np(value) -> Decimal:
    from .storage import normalize_points
    return normalize_points(value)


def _db(value) -> float:
    from .storage import points_to_db
    return points_to_db(value)


INVITE_REWARD_FIELDS = {
    "premium_msg_threshold": "INTEGER NOT NULL DEFAULT 0",
    "premium_msg_points": "REAL NOT NULL DEFAULT 0",
    "premium_boost_points": "REAL NOT NULL DEFAULT 0",
    "normal_msg_threshold": "INTEGER NOT NULL DEFAULT 0",
    "normal_msg_points": "REAL NOT NULL DEFAULT 0",
}


class FeatureStoreMixin:
    # ---- migrations -------------------------------------------------------

    def _migrate_features(self, conn: sqlite3.Connection) -> None:
        ensure = self._ensure_column  # type: ignore[attr-defined]
        ensure(conn, "group_activity_users", "prev_counted_ts", "REAL NOT NULL DEFAULT 0")
        for column, definition in INVITE_REWARD_FIELDS.items():
            ensure(conn, "group_invite_config", column, definition)
        for column, definition in {
            "is_premium": "INTEGER NOT NULL DEFAULT 0",
            "effective_messages": "INTEGER NOT NULL DEFAULT 0",
            "msg_reward": "REAL NOT NULL DEFAULT 0",
            "msg_rewarded_at": "TEXT",
        }.items():
            ensure(conn, "group_invite_joins", column, definition)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS point_activity_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                messages INTEGER NOT NULL,
                points REAL NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, messages)
            );
            CREATE TABLE IF NOT EXISTS point_tier_awards (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                tier_messages INTEGER NOT NULL,
                points REAL NOT NULL DEFAULT 0,
                awarded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(chat_id, user_id, day, tier_messages)
            );
            CREATE TABLE IF NOT EXISTS invite_boost_rewards (
                chat_id INTEGER NOT NULL,
                boost_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                inviter_id INTEGER NOT NULL,
                points REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                removed_at TEXT,
                PRIMARY KEY(chat_id, boost_id)
            );
            CREATE INDEX IF NOT EXISTS idx_invite_boost_user
                ON invite_boost_rewards(chat_id, user_id, removed_at);
            CREATE TABLE IF NOT EXISTS raffle_win_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                raffle_id INTEGER NOT NULL DEFAULT 0,
                won_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_raffle_win_log
                ON raffle_win_log(chat_id, user_id, won_at);
            """
        )
        if not conn.execute("SELECT 1 FROM raffle_win_log LIMIT 1").fetchone():
            conn.execute(
                """INSERT INTO raffle_win_log (chat_id, user_id, raffle_id, won_at)
                   SELECT r.chat_id, w.user_id, r.id, COALESCE(r.drawn_at, r.ends_at)
                   FROM raffle_winners w JOIN raffles r ON r.id=w.raffle_id"""
            )

    def log_raffle_wins(self, conn: sqlite3.Connection, raffle_id: int, user_ids) -> None:
        row = conn.execute("SELECT chat_id FROM raffles WHERE id=?", (raffle_id,)).fetchone()
        if not row:
            return
        conn.executemany(
            "INSERT INTO raffle_win_log (chat_id, user_id, raffle_id) VALUES (?, ?, ?)",
            ((int(row["chat_id"]), int(uid), raffle_id) for uid in user_ids),
        )
        conn.execute("DELETE FROM raffle_win_log WHERE won_at<DATETIME('now','-400 days')")

    # ---- 活跃阶梯奖励 ------------------------------------------------------

    def activity_tiers(self, chat_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:  # type: ignore[attr-defined]
            return conn.execute(
                """SELECT * FROM point_activity_tiers WHERE chat_id=?
                   ORDER BY messages, id""",
                (chat_id,),
            ).fetchall()

    def set_activity_tier(self, chat_id: int, messages: int, points, created_by: int) -> int:
        """Add or update the tier for ``messages`` effective messages."""
        if isinstance(messages, bool) or not isinstance(messages, int) or not 1 <= messages <= 100000:
            raise ValueError("阶梯条数范围为1-100000")
        amount = _np(points)
        if amount <= 0 or amount > 1000000:
            raise ValueError("阶梯奖励积分范围为0.01-1000000")
        with self.connect() as conn:  # type: ignore[attr-defined]
            count = int(conn.execute(
                "SELECT COUNT(*) FROM point_activity_tiers WHERE chat_id=? AND messages<>?",
                (chat_id, messages),
            ).fetchone()[0])
            if count >= 20:
                raise ValueError("每个群最多设置 20 档阶梯奖励")
            conn.execute(
                """INSERT INTO point_activity_tiers (chat_id, messages, points, created_by)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id, messages) DO UPDATE SET
                     points=excluded.points, created_by=excluded.created_by""",
                (chat_id, messages, _db(amount), created_by),
            )
            row = conn.execute(
                "SELECT id FROM point_activity_tiers WHERE chat_id=? AND messages=?",
                (chat_id, messages),
            ).fetchone()
            return int(row["id"])

    def delete_activity_tier(self, chat_id: int, tier_id: int) -> bool:
        with self.connect() as conn:  # type: ignore[attr-defined]
            cursor = conn.execute(
                "DELETE FROM point_activity_tiers WHERE chat_id=? AND id=?",
                (chat_id, tier_id),
            )
            return cursor.rowcount > 0

    def clear_activity_tiers(self, chat_id: int) -> int:
        with self.connect() as conn:  # type: ignore[attr-defined]
            return conn.execute(
                "DELETE FROM point_activity_tiers WHERE chat_id=?", (chat_id,)
            ).rowcount

    def award_activity_tiers(
        self, chat_id: int, user_id: int, username: str, display_name: str,
    ) -> list[tuple[int, Decimal, Decimal]]:
        """Award every reached tier once per Beijing day.

        Returns [(tier_messages, points, balance_after), ...]."""
        config = self.points_config(chat_id)  # type: ignore[attr-defined]
        if not config["is_enabled"]:
            return []
        awarded: list[tuple[int, Decimal, Decimal]] = []
        with self.connect() as conn:  # type: ignore[attr-defined]
            tiers = conn.execute(
                "SELECT messages, points FROM point_activity_tiers WHERE chat_id=? ORDER BY messages",
                (chat_id,),
            ).fetchall()
            if not tiers:
                return []
            row = conn.execute(
                """SELECT active_messages FROM group_activity_users
                   WHERE chat_id=? AND user_id=? AND day=DATE('now','+8 hours')""",
                (chat_id, user_id),
            ).fetchone()
            current = int(row["active_messages"]) if row else 0
            for tier in tiers:
                need = int(tier["messages"])
                if current < need:
                    break
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO point_tier_awards
                       (chat_id, user_id, day, tier_messages, points)
                       VALUES (?, ?, DATE('now','+8 hours'), ?, ?)""",
                    (chat_id, user_id, need, float(tier["points"] or 0)),
                )
                if not cursor.rowcount:
                    continue
                points = _np(tier["points"] or 0)
                balance = self._adjust_points_conn(  # type: ignore[attr-defined]
                    conn, chat_id, user_id, points, f"活跃阶梯奖励（{need}条）", 0,
                    username, display_name,
                )
                awarded.append((need, points, balance))
            conn.execute(
                "DELETE FROM point_tier_awards WHERE day < DATE('now','+8 hours','-40 days')"
            )
        return awarded

    # ---- 邀请发言/助推奖励 --------------------------------------------------

    def update_invite_rewards(self, chat_id: int, updated_by: int, **values) -> None:
        self.invite_config(chat_id)  # type: ignore[attr-defined]
        fields, params = [], []
        for key, value in values.items():
            if key not in INVITE_REWARD_FIELDS:
                raise ValueError("未知邀请奖励设置")
            if key.endswith("_threshold"):
                value = int(value)
                if not 0 <= value <= 100000:
                    raise ValueError("发言条数范围为0-100000")
            else:
                value = _db(value)
                if value < 0 or value > 1000000:
                    raise ValueError("奖励积分范围为0-1000000")
            fields.append(f"{key}=?")
            params.append(value)
        if not fields:
            return
        params.extend((updated_by, chat_id))
        with self.connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                f"""UPDATE group_invite_config SET {', '.join(fields)}, updated_by=?,
                    updated_at=CURRENT_TIMESTAMP WHERE chat_id=?""",
                params,
            )

    def mark_invitee_premium(self, chat_id: int, user_id: int, premium: bool) -> None:
        if not premium:
            return
        with self.connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                """UPDATE group_invite_joins SET is_premium=1
                   WHERE chat_id=? AND user_id=? AND left_at IS NULL""",
                (chat_id, user_id),
            )

    def award_invite_message_reward(self, chat_id: int, user_id: int):
        """Pay the inviter once when the invitee reaches the effective-message
        threshold. Returns (join_row, points, balance, threshold) or None."""
        config = self.invite_config(chat_id)  # type: ignore[attr-defined]
        if not self.points_config(chat_id)["is_enabled"]:  # type: ignore[attr-defined]
            return None
        keys = set(config.keys())
        with self.connect() as conn:  # type: ignore[attr-defined]
            join = conn.execute(
                """SELECT * FROM group_invite_joins
                   WHERE chat_id=? AND user_id=? AND left_at IS NULL""",
                (chat_id, user_id),
            ).fetchone()
            if not join or join["msg_rewarded_at"]:
                return None
            premium = bool(int(join["is_premium"] or 0))
            prefix = "premium" if premium else "normal"
            if f"{prefix}_msg_points" not in keys:
                return None
            points = _np(config[f"{prefix}_msg_points"] or 0)
            threshold = max(1, int(config[f"{prefix}_msg_threshold"] or 0))
            if points <= 0 or int(join["effective_messages"] or 0) < threshold:
                return None
            cursor = conn.execute(
                """UPDATE group_invite_joins SET msg_reward=?, msg_rewarded_at=CURRENT_TIMESTAMP
                   WHERE chat_id=? AND user_id=? AND left_at IS NULL AND msg_rewarded_at IS NULL""",
                (_db(points), chat_id, user_id),
            )
            if not cursor.rowcount:
                return None
            inviter_id = int(join["inviter_id"])
            inviter = conn.execute(
                "SELECT username, display_name FROM group_invite_links WHERE id=?",
                (int(join["link_id"]),),
            ).fetchone()
            kind = "会员" if premium else "成员"
            balance = self._adjust_points_conn(  # type: ignore[attr-defined]
                conn, chat_id, inviter_id, points,
                f"邀请{kind} {user_id} 有效发言满{threshold}条", 0,
                str(inviter["username"] or "") if inviter else "",
                str(inviter["display_name"] or "") if inviter else "",
            )
            return join, points, balance, threshold

    def award_invite_boost(self, chat_id: int, boost_id: str, user_id: int):
        """A boost from an invited member. Returns (join_row, points, balance) or None."""
        config = self.invite_config(chat_id)  # type: ignore[attr-defined]
        if not self.points_config(chat_id)["is_enabled"]:  # type: ignore[attr-defined]
            return None
        points = _np(config["premium_boost_points"] or 0) if "premium_boost_points" in config.keys() else _np(0)
        if points <= 0 or not boost_id:
            return None
        with self.connect() as conn:  # type: ignore[attr-defined]
            join = conn.execute(
                """SELECT * FROM group_invite_joins
                   WHERE chat_id=? AND user_id=? AND left_at IS NULL""",
                (chat_id, user_id),
            ).fetchone()
            if not join:
                return None
            inviter_id = int(join["inviter_id"])
            cursor = conn.execute(
                """INSERT OR IGNORE INTO invite_boost_rewards
                   (chat_id, boost_id, user_id, inviter_id, points)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, str(boost_id)[:200], user_id, inviter_id, _db(points)),
            )
            if not cursor.rowcount:
                return None
            conn.execute(
                "UPDATE group_invite_joins SET is_premium=1 WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            inviter = conn.execute(
                "SELECT username, display_name FROM group_invite_links WHERE id=?",
                (int(join["link_id"]),),
            ).fetchone()
            balance = self._adjust_points_conn(  # type: ignore[attr-defined]
                conn, chat_id, inviter_id, points, f"邀请会员 {user_id} 助推本群", 0,
                str(inviter["username"] or "") if inviter else "",
                str(inviter["display_name"] or "") if inviter else "",
            )
            return join, points, balance

    def revoke_invite_boost(self, chat_id: int, boost_id: str):
        """Boost removed/expired: claw back. Returns (row, points, balance) or None."""
        with self.connect() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                """SELECT * FROM invite_boost_rewards
                   WHERE chat_id=? AND boost_id=? AND removed_at IS NULL""",
                (chat_id, str(boost_id)[:200]),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE invite_boost_rewards SET removed_at=CURRENT_TIMESTAMP
                   WHERE chat_id=? AND boost_id=?""",
                (chat_id, str(boost_id)[:200]),
            )
            points = _np(row["points"] or 0)
            balance = self._adjust_points_conn(  # type: ignore[attr-defined]
                conn, chat_id, int(row["inviter_id"]), -points,
                f"邀请会员 {row['user_id']} 取消助推，扣回助推奖励", 0,
                allow_negative=True,
            )
            return row, points, balance

    def revoke_invite_extra_rewards(self, chat_id: int, user_id: int, inviter_id: int,
                                    msg_reward=0) -> Decimal:
        """Invitee left/kicked: claw back message reward + active boost rewards.
        Returns total clawed back (positive number)."""
        total = _np(0)
        with self.connect() as conn:  # type: ignore[attr-defined]
            msg_points = _np(msg_reward or 0)
            if msg_points > 0:
                self._adjust_points_conn(  # type: ignore[attr-defined]
                    conn, chat_id, inviter_id, -msg_points,
                    f"邀请成员 {user_id} 退出群组，扣回发言奖励", 0, allow_negative=True,
                )
                total += msg_points
            rows = conn.execute(
                """SELECT * FROM invite_boost_rewards
                   WHERE chat_id=? AND user_id=? AND removed_at IS NULL""",
                (chat_id, user_id),
            ).fetchall()
            for row in rows:
                points = _np(row["points"] or 0)
                conn.execute(
                    """UPDATE invite_boost_rewards SET removed_at=CURRENT_TIMESTAMP
                       WHERE chat_id=? AND boost_id=?""",
                    (chat_id, str(row["boost_id"])),
                )
                if points > 0:
                    self._adjust_points_conn(  # type: ignore[attr-defined]
                        conn, chat_id, int(row["inviter_id"]), -points,
                        f"邀请会员 {user_id} 退出群组，扣回助推奖励", 0, allow_negative=True,
                    )
                    total += points
        return total

    # ---- 抽奖开奖历史（开奖名额分配用） ----------------------------------------

    def raffle_win_history(self, chat_id: int, user_ids: list[int], days: int = 30) -> dict[int, tuple[int, int]]:
        """{user_id: (wins in last ``days`` days, wins ever)} in this group."""
        result = {int(uid): (0, 0) for uid in user_ids}
        if not user_ids:
            return result
        with self.connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"""SELECT user_id,
                          SUM(CASE WHEN won_at>=DATETIME('now', ?) THEN 1 ELSE 0 END) AS recent,
                          COUNT(*) AS total
                   FROM raffle_win_log
                   WHERE chat_id=? AND user_id IN ({','.join('?' * len(user_ids))})
                   GROUP BY user_id""",
                (f"-{int(days)} days", chat_id, *[int(uid) for uid in user_ids]),
            ).fetchall()
        for row in rows:
            result[int(row["user_id"])] = (int(row["recent"] or 0), int(row["total"] or 0))
        return result

    def recent_joiners(self, chat_id: int, user_ids: list[int], days: int = 7) -> set[int]:
        if not user_ids:
            return set()
        marks = ",".join("?" * len(user_ids))
        ids = [int(uid) for uid in user_ids]
        with self.connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"""SELECT user_id FROM group_operations
                    WHERE chat_id=? AND action='join' AND created_at>=DATETIME('now', ?)
                      AND user_id IN ({marks})
                    UNION
                    SELECT user_id FROM group_invite_joins
                    WHERE chat_id=? AND joined_at>=DATETIME('now', ?) AND user_id IN ({marks})""",
                (chat_id, f"-{int(days)} days", *ids, chat_id, f"-{int(days)} days", *ids),
            ).fetchall()
        return {int(row["user_id"]) for row in rows}

    def latest_universal_raffle(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:  # type: ignore[attr-defined]
            return conn.execute(
                """SELECT * FROM raffles WHERE chat_id=? AND raffle_type='universal'
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (chat_id,),
            ).fetchone()
