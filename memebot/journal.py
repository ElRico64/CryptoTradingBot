"""SQLite trade journal: every signal, fill and position, so results can be audited and learned from."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .models import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, chain TEXT, address TEXT, symbol TEXT,
    score REAL, decision TEXT, reason TEXT,
    sources TEXT, features TEXT, components TEXT,
    position_id TEXT, outcome_multiple REAL
);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY, status TEXT, opened_ts REAL, closed_ts REAL,
    pnl_usd REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, position_id TEXT, side TEXT, chain TEXT, address TEXT, symbol TEXT,
    qty REAL, usd REAL, price REAL, tx TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_pos ON signals(position_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""


class Journal:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def log_signal(self, chain: str, address: str, symbol: str, score: float, decision: str, reason: str,
                   sources: list[str], features: dict[str, Any], components: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO signals (ts, chain, address, symbol, score, decision, reason, sources, features, components)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), chain, address, symbol, score, decision, reason,
             json.dumps(sources), json.dumps(features), json.dumps(components)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def attach_position(self, signal_id: int, position_id: str) -> None:
        self.db.execute("UPDATE signals SET position_id=? WHERE id=?", (position_id, signal_id))
        self.db.commit()

    def save_position(self, pos: Position) -> None:
        self.db.execute(
            "INSERT INTO positions (id, status, opened_ts, closed_ts, pnl_usd, data) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET status=excluded.status, closed_ts=excluded.closed_ts,"
            " pnl_usd=excluded.pnl_usd, data=excluded.data",
            (pos.id, pos.status, pos.entry_ts, pos.closed_ts, pos.realized_usd - pos.cost_usd if pos.status == "closed" else None,
             json.dumps(pos.to_dict())),
        )
        if pos.status == "closed":
            self.db.execute("UPDATE signals SET outcome_multiple=? WHERE position_id=?", (pos.realized_multiple, pos.id))
        self.db.commit()

    def open_positions(self) -> list[Position]:
        rows = self.db.execute("SELECT data FROM positions WHERE status='open'").fetchall()
        return [Position.from_dict(json.loads(r["data"])) for r in rows]

    def log_trade(self, pos: Position, side: str, qty: float, usd: float, price: float, tx: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO trades (ts, position_id, side, chain, address, symbol, qty, usd, price, tx, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), pos.id, side, pos.chain, pos.token_address, pos.symbol, qty, usd, price, tx, reason),
        )
        self.db.commit()

    def training_rows(self) -> list[tuple[dict[str, float], dict[str, float], float]]:
        rows = self.db.execute(
            "SELECT features, components, outcome_multiple FROM signals WHERE outcome_multiple IS NOT NULL"
        ).fetchall()
        return [(json.loads(r["features"]), json.loads(r["components"]), float(r["outcome_multiple"])) for r in rows]

    def stats(self) -> dict[str, Any]:
        closed = self.db.execute("SELECT pnl_usd, data FROM positions WHERE status='closed'").fetchall()
        pnls = [float(r["pnl_usd"] or 0) for r in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        mults = [Position.from_dict(json.loads(r["data"])).realized_multiple for r in closed]
        n_signals = self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        n_entered = self.db.execute("SELECT COUNT(*) FROM signals WHERE decision='enter'").fetchone()[0]
        return {
            "signals_analysed": n_signals,
            "signals_entered": n_entered,
            "closed_trades": len(pnls),
            "win_rate": len(wins) / len(pnls) if pnls else 0.0,
            "total_pnl_usd": sum(pnls),
            "avg_win_usd": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss_usd": sum(losses) / len(losses) if losses else 0.0,
            "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf") if wins else 0.0,
            "best_multiple": max(mults) if mults else 0.0,
            "hit_2x": sum(1 for r in closed if 2.0 in Position.from_dict(json.loads(r["data"])).tp_hits),
        }

    def recent_trades(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
