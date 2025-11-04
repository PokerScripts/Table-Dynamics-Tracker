#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dynamics.py — Table Dynamics Tracker
Описание:
  Скрипт для анализа динамики покерных статов по игрокам
  из Hand History (PokerStars) или CSV.
  Версия: MVP (v1.0)
"""

import re
import os
import sys
import argparse
import json
import csv
from collections import defaultdict, deque
from datetime import datetime
from statistics import mean

# -----------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ И ФУНКЦИИ
# -----------------------------------------------------------

def parse_args():
    """Разбор аргументов командной строки"""
    p = argparse.ArgumentParser(description="Покерный анализатор динамики (Table-Dynamics-Tracker)")
    p.add_argument("--hh", help="Путь к файлу или директории с Hand History")
    p.add_argument("--window-hands", type=int, default=30, help="Размер окна в руках (по умолчанию 30)")
    p.add_argument("--player", help="Фокус на конкретного игрока")
    p.add_argument("--export-csv", help="Экспорт результатов в CSV")
    p.add_argument("--export-json", help="Экспорт результатов в JSON")
    p.add_argument("--ascii", action="store_true", help="ASCII-графики (упрощённые)")
    p.add_argument("--quiet", action="store_true", help="Минимум логов")
    return p.parse_args()


def log(msg, quiet=False):
    """Простая печать логов"""
    if not quiet:
        print(msg)


def read_hands_from_file(path):
    """Генератор, который читает HH-файл PokerStars и возвращает список строк для каждой раздачи"""
    with open(path, encoding="utf-8", errors="ignore") as f:
        buf = []
        for line in f:
            if line.startswith("PokerStars Hand"):
                if buf:
                    yield buf
                    buf = []
            buf.append(line.strip())
        if buf:
            yield buf


def list_hh_files(path):
    """Возвращает список файлов .txt в директории (или сам файл, если это файл)"""
    if os.path.isfile(path):
        return [path]
    files = []
    for root, _, fs in os.walk(path):
        for f in fs:
            if f.lower().endswith(".txt"):
                files.append(os.path.join(root, f))
    return files


# -----------------------------------------------------------
# ПАРСЕР HH (УПРОЩЕННЫЙ)
# -----------------------------------------------------------

class Hand:
    """Модель одной раздачи"""
    def __init__(self, hand_id, players, actions, winner, potsize, timestamp):
        self.hand_id = hand_id
        self.players = players  # {name: stack_bb}
        self.actions = actions  # list of (street, player, action)
        self.winner = winner
        self.potsize = potsize
        self.timestamp = timestamp


def parse_pokerstars_hand(lines):
    """Упрощённый парсер PokerStars HH"""
    hand_id = None
    timestamp = None
    players = {}
    actions = []
    winner = None
    potsize = 0

    # ID и время
    m = re.match(r"PokerStars Hand #(\d+):", lines[0])
    if m:
        hand_id = m.group(1)
    for line in lines:
        if "UTC" in line and ":" in line:
            try:
                timestamp = datetime.strptime(line.split("UTC")[0].strip(), "%Y/%m/%d %H:%M:%S")
            except Exception:
                pass
        # Игроки
        if line.startswith("Seat "):
            m = re.match(r"Seat \d+: ([^\(]+) \(\s*([\d\.]+)\s+in chips\)", line)
            if m:
                name, chips = m.groups()
                players[name.strip()] = float(chips)
        # Действия
        if ":" in line and ("folds" in line or "calls" in line or "bets" in line or "raises" in line or "checks" in line):
            parts = line.split(":", 1)
            player = parts[0].strip()
            action = parts[1].strip()
            street = "PREFLOP"
            if "*** FLOP" in "\n".join(lines):
                # Простейшая эвристика, не 100% точно
                if "*** FLOP" in line:
                    street = "FLOP"
            actions.append((street, player, action))
        # Победитель
        if "collected" in line and "from pot" in line:
            m = re.match(r"(.+?) collected", line)
            if m:
                winner = m.group(1).strip()
        # Пот
        if "Total pot" in line:
            m = re.search(r"Total pot ([\d\.]+)", line)
            if m:
                potsize = float(m.group(1))
    if not hand_id or not players:
        return None
    return Hand(hand_id, players, actions, winner, potsize, timestamp)


# -----------------------------------------------------------
# СТАТИСТИКА ПО ИГРОКАМ
# -----------------------------------------------------------

class PlayerStats:
    """Хранит счётчики действий игрока"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.hands = 0
        self.vpip = 0
        self.pfr = 0
        self._3bet_opps = 0
        self._3bet = 0
        self.af_acts = {"bet":0, "raise":0, "call":0}
        self.showdown = 0
        self.won_showdown = 0
        self.bb_won = 0.0

    def update_from_hand(self, hand: Hand, bb_value=1.0):
        """Обновляем статы по одной раздаче"""
        if hand.players.get(self.name, None) is None:
            return
        self.hands += 1
        # VPIP/PFR
        for street, pl, act in hand.actions:
            if pl != self.name:
                continue
            if "calls" in act or "raises" in act or "bets" in act:
                if street == "PREFLOP":
                    self.vpip += 1
            if "raises" in act and street == "PREFLOP":
                self.pfr += 1
            if "raises" in act and street == "PREFLOP":
                self._3bet += 1  # упрощённо
            if "calls" in act and street != "PREFLOP":
                self.af_acts["call"] += 1
            if "bets" in act:
                self.af_acts["bet"] += 1
            if "raises" in act and street != "PREFLOP":
                self.af_acts["raise"] += 1
        if hand.winner == self.name:
            self.won_showdown += 1
            self.bb_won += hand.potsize / bb_value
        # Примем, что любой выигрыш = дошёл до вскрытия (упрощённо)
        if hand.winner == self.name:
            self.showdown += 1

    def snapshot(self):
        """Возвращает словарь текущих метрик"""
        af_calls = self.af_acts["call"]
        af_bets = self.af_acts["bet"]
        af_raises = self.af_acts["raise"]
        af = (af_bets + af_raises) / af_calls if af_calls > 0 else None
        afq = (af_bets + af_raises) / max(1, (af_bets + af_raises + af_calls))
        return {
            "hands": self.hands,
            "VPIP%": round(100 * self.vpip / self.hands, 1) if self.hands else 0,
            "PFR%": round(100 * self.pfr / self.hands, 1) if self.hands else 0,
            "AF": round(af, 2) if af is not None else "-",
            "AFq%": round(100 * afq, 1),
            "W$SD%": round(100 * self.won_showdown / self.hands, 1) if self.hands else 0,
            "BB/100": round(self.bb_won / self.hands * 100, 2) if self.hands else 0,
        }


# -----------------------------------------------------------
# ОСНОВНОЙ ДВИЖОК
# -----------------------------------------------------------

class DynamicsEngine:
    def __init__(self, window_hands=30):
        self.window_hands = window_hands
        self.hands = deque(maxlen=window_hands)
        self.players = defaultdict(PlayerStats)
        self.snapshots = []

    def process_hand(self, hand: Hand):
        """Добавляем новую руку в поток"""
        self.hands.append(hand)
        # Обновляем все статы
        current_players = list(hand.players.keys())
        for name in current_players:
            ps = self.players[name]
            ps.name = name
            ps.update_from_hand(hand)
        # Делаем снимок (rolling)
        snap = self.make_snapshot()
        self.snapshots.append((hand.hand_id, snap))

    def make_snapshot(self):
        """Создаёт текущий снимок метрик"""
        snapshot = {}
        for name, ps in self.players.items():
            snapshot[name] = ps.snapshot()
        return snapshot


# -----------------------------------------------------------
# ВЫВОД
# -----------------------------------------------------------

def print_table(snapshot, quiet=False):
    """Вывод таблицы метрик"""
    headers = ["Player", "Hands", "VPIP%", "PFR%", "AF", "AFq%", "W$SD%", "BB/100"]
    print("\n" + " | ".join(headers))
    print("-" * 65)
    for name, stats in snapshot.items():
        print(f"{name:15} | {stats['hands']:5} | {stats['VPIP%']:5} | {stats['PFR%']:5} | {stats['AF']:>4} | {stats['AFq%']:5} | {stats['W$SD%']:5} | {stats['BB/100']:>6}")


def export_csv(snapshots, path):
    """Экспорт всех снимков в CSV"""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["hand_id", "player", "hands", "VPIP%", "PFR%", "AF", "AFq%", "W$SD%", "BB/100"])
        for hand_id, snap in snapshots:
            for name, stats in snap.items():
                writer.writerow([hand_id, name, stats["hands"], stats["VPIP%"], stats["PFR%"], stats["AF"], stats["AFq%"], stats["W$SD%"], stats["BB/100"]])


def export_json(snapshots, path):
    """Экспорт в JSON"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"snapshots": snapshots}, f, indent=2, ensure_ascii=False)


# -----------------------------------------------------------
# MAIN
# -----------------------------------------------------------

def main():
    args = parse_args()
    if not args.hh:
        print("Укажите --hh путь к HH файлам")
        sys.exit(1)

    engine = DynamicsEngine(window_hands=args.window_hands)
    files = list_hh_files(args.hh)
    log(f"Найдено файлов: {len(files)}", args.quiet)

    for fn in files:
        for lines in read_hands_from_file(fn):
            hand = parse_pokerstars_hand(lines)
            if hand:
                engine.process_hand(hand)

    if not engine.snapshots:
        print("Нет обработанных рук")
        return

    last_id, last_snap = engine.snapshots[-1]
    print(f"\n=== Table Dynamics Snapshot (последние {args.window_hands} рук) ===")
    print_table(last_snap, args.quiet)

    if args.export_csv:
        export_csv(engine.snapshots, args.export_csv)
        print(f"Экспортировано в {args.export_csv}")
    if args.export_json:
        export_json(engine.snapshots, args.export_json)
        print(f"Экспортировано в {args.export_json}")


if __name__ == "__main__":
    main()