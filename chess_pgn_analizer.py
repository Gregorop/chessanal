import sys
import re
from collections import Counter
from pathlib import Path

import chess
import chess.pgn
import chess.svg
import chess.engine

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTableWidget, QTableWidgetItem,
    QTextEdit, QFileDialog, QMessageBox, QHeaderView, QSplitter,
    QGroupBox, QAbstractItemView
)
from PyQt5.QtSvg import QSvgWidget
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont


# ============================================================
# Обход всего дерева PGN (включая варианты в скобках)
# ============================================================
def iter_game_tree(game):
    def recurse(node, board):
        for child in node.variations:
            move = child.move
            yield board.copy(), move, child
            new_board = board.copy()
            new_board.push(move)
            yield from recurse(child, new_board)

    yield from recurse(game, game.board())


# ============================================================
# Поток анализа движком (multipv=3)
# ============================================================
class EngineAnalysisThread(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, results, engine_path, depth=12, multipv=3):
        super().__init__()
        self.results = results
        self.engine_path = engine_path
        self.depth = depth
        self.multipv = multipv

    def run(self):
        try:
            engine = chess.engine.SimpleEngine.popen_uci(self.engine_path)
        except Exception as e:
            self.error.emit(f"Не удалось запустить движок: {e}")
            return

        analysis_data = {}
        try:
            for idx, res in enumerate(self.results):
                board = chess.Board(res["fen"])
                infos = engine.analyse(
                    board,
                    chess.engine.Limit(depth=self.depth),
                    multipv=self.multipv
                )

                candidates = []
                for info in infos:
                    score = info["score"].white().score(mate_score=10000)
                    if score is None:
                        score = 0
                    pv = info.get("pv", [])
                    if not pv:
                        continue
                    move = pv[0]
                    try:
                        san = board.san(move)
                    except Exception:
                        san = move.uci()
                    candidates.append({
                        "san": san,
                        "uci": move.uci(),
                        "score": score
                    })

                best_score = candidates[0]["score"] if candidates else 0

                analysis_data[idx] = {
                    "score": best_score,
                    "candidates": candidates,
                    "best_move": candidates[0]["san"] if candidates else None,
                    "best_move_uci": candidates[0]["uci"] if candidates else None
                }
        except Exception as e:
            self.error.emit(f"Ошибка анализа: {e}")
        finally:
            engine.quit()

        self.finished.emit(analysis_data)


# ============================================================
# Основное окно
# ============================================================
class ChessPGNAnalyzerQt(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Chess PGN Analyzer (PyQt5)")
        self.setGeometry(100, 100, 1300, 850)

        self.games = []
        self.results = []
        self.analysis_data = {}
        self.current_fen = ""
        self.repertoire_colour = None

        self.engine_path = self.find_stockfish()
        self.analysis_thread = None

        self.init_ui()
        self.apply_style()

    def find_stockfish(self):
        import shutil
        path = shutil.which("stockfish")
        if path:
            return path
        for name in ("stockfish.exe", "stockfish"):
            local = Path(name)
            if local.exists():
                return str(local)
        return "stockfish"

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f0f0f0; }
            QGroupBox { font-weight: bold; border: 1px solid #aaa; border-radius: 5px; margin-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QPushButton { padding: 6px 12px; border-radius: 4px; background-color: #e0e0e0; }
            QPushButton:hover { background-color: #d0d0d0; }
            QTableWidget { selection-background-color: #a0c0ff; }
            QTextEdit { font-family: Consolas, monospace; }
        """)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # --- Верхняя панель ---
        top_bar = QHBoxLayout()
        self.btn_open = QPushButton("📂 Открыть PGN")
        self.btn_open.clicked.connect(self.load_pgn)
        top_bar.addWidget(self.btn_open)

        self.lbl_file = QLabel("Файл не загружен")
        top_bar.addWidget(self.lbl_file)
        top_bar.addStretch()

        self.lbl_engine = QLabel(f"Движок: {self.engine_path}")
        self.lbl_engine.setStyleSheet("color: #666;")
        top_bar.addWidget(self.lbl_engine)
        main_layout.addLayout(top_bar)

        # --- Поиск ---
        search_group = QGroupBox("Поиск хода (SAN)")
        search_layout = QHBoxLayout(search_group)
        search_layout.addWidget(QLabel("Ход:"))
        self.entry_move = QLineEdit()
        self.entry_move.setPlaceholderText("Nf3, e4, O-O...")
        self.entry_move.returnPressed.connect(self.search_move)
        search_layout.addWidget(self.entry_move)

        self.btn_search = QPushButton("Найти")
        self.btn_search.clicked.connect(self.search_move)
        search_layout.addWidget(self.btn_search)

        self.lbl_status = QLabel("Загрузите PGN")
        search_layout.addWidget(self.lbl_status)
        main_layout.addWidget(search_group)

        # --- Внешний сплиттер: доска | правая колонка ---
        outer_splitter = QSplitter(Qt.Horizontal)

        # Левая часть: доска + FEN
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.addWidget(QLabel("Позиция (после хода)", font=QFont("Arial", 12, QFont.Bold)))
        self.svg_widget = QSvgWidget()
        self.svg_widget.setMinimumSize(480, 480)
        left_layout.addWidget(self.svg_widget)

        self.lbl_position = QLabel("Выберите совпадение")
        left_layout.addWidget(self.lbl_position)

        fen_layout = QHBoxLayout()
        fen_layout.addWidget(QLabel("FEN:"))
        self.fen_edit = QLineEdit()
        self.fen_edit.setReadOnly(True)
        fen_layout.addWidget(self.fen_edit)
        self.btn_copy_fen = QPushButton("📋 Копировать")
        self.btn_copy_fen.clicked.connect(self.copy_fen)
        fen_layout.addWidget(self.btn_copy_fen)
        left_layout.addLayout(fen_layout)
        outer_splitter.addWidget(left_widget)

        # Правая колонка: внутренний вертикальный сплиттер (таблица / статистика)
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        inner_splitter = QSplitter(Qt.Vertical)

        # Верх правой колонки: таблица
        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(QLabel("Найденные совпадения", font=QFont("Arial", 12, QFont.Bold)))

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Партия", "№ хода", "Сторона", "Найденный ход", "Ответ", "Оценка (cp)"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.on_table_select)
        table_layout.addWidget(self.table)
        inner_splitter.addWidget(table_widget)

        # Низ правой колонки: статистика во всю высоту
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(0, 0, 0, 0)

        stats_header = QLabel("Статистика и рекомендации движка", font=QFont("Arial", 12, QFont.Bold))
        stats_layout.addWidget(stats_header)

        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        # Убрали setMaximumHeight — теперь растягивается
        stats_layout.addWidget(self.stats_text)
        inner_splitter.addWidget(stats_widget)

        # Пропорции: таблица 55%, статистика 45%
        inner_splitter.setSizes([550, 450])

        right_layout.addWidget(inner_splitter)
        outer_splitter.addWidget(right_widget)

        # Пропорции: доска ~45%, правая колонка ~55%
        outer_splitter.setSizes([520, 760])
        main_layout.addWidget(outer_splitter, stretch=1)

    def load_pgn(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите PGN", "", "PGN files (*.pgn);;All files (*.*)"
        )
        if not path:
            return

        self.games = []
        self.repertoire_colour = None

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                while True:
                    game = chess.pgn.read_game(f)
                    if game is None:
                        break
                    self.games.append(game)
                    colour = game.headers.get("ChesstempoRepertoireColour", "").lower()
                    if colour in ("white", "black"):
                        self.repertoire_colour = colour
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))
            return

        self.lbl_file.setText(f"{Path(path).name} | Партий: {len(self.games)}")

        if self.repertoire_colour:
            own = "белых" if self.repertoire_colour == "white" else "чёрных"
            opp = "чёрных" if self.repertoire_colour == "white" else "белых"
            self.lbl_status.setText(
                f"Загружено {len(self.games)} партий | Репертуар за {own} "
                f"(ищем ходы {opp})"
            )
        else:
            self.lbl_status.setText(f"Загружено {len(self.games)} партий")

        self.table.setRowCount(0)
        self.stats_text.clear()
        self.results = []
        self.analysis_data = {}

    def normalize_san(self, san):
        san = san.strip()
        replacements = {
            "С": "B", "с": "B", "C": "B", "c": "B",
            "К": "N", "к": "N",
            "Л": "R", "л": "R",
            "Ф": "Q", "ф": "Q", "Д": "Q", "д": "Q"
        }
        for old, new in replacements.items():
            san = san.replace(old, new)
        san = re.sub(r"[+#?!]+$", "", san)
        san = san.replace("0", "O")
        return san

    def search_move(self):
        if not self.games:
            QMessageBox.warning(self, "Внимание", "Сначала загрузите PGN")
            return

        query = self.entry_move.text().strip()
        if not query:
            return
        query = self.normalize_san(query)

        search_white = None
        if self.repertoire_colour == "black":
            search_white = True
        elif self.repertoire_colour == "white":
            search_white = False

        self.results = []
        self.table.setRowCount(0)
        self.stats_text.clear()
        self.analysis_data = {}
        self.current_fen = ""
        self.fen_edit.clear()
        self.lbl_position.setText("Выберите совпадение")

        for game_idx, game in enumerate(self.games, start=1):
            for board_before, move, node in iter_game_tree(game):
                is_white_move = (board_before.turn == chess.WHITE)
                if search_white is not None and is_white_move != search_white:
                    continue

                try:
                    actual_san = board_before.san(move)
                except Exception:
                    continue

                normalized = self.normalize_san(actual_san)
                if normalized != query:
                    continue

                move_number = board_before.fullmove_number
                side = "Белые" if is_white_move else "Чёрные"

                board_after = board_before.copy()
                board_after.push(move)
                fen_after = board_after.fen()

                reply = "(конец варианта)"
                if node.variations:
                    reply_move = node.variations[0].move
                    try:
                        reply = board_after.san(reply_move)
                    except Exception:
                        reply = reply_move.uci()

                self.results.append({
                    "game": game_idx,
                    "move_number": move_number,
                    "side": side,
                    "played": actual_san,
                    "reply": reply,
                    "fen": fen_after,
                    "lastmove": move.uci()
                })

        self.show_results()
        if self.results:
            self.analyze_positions()

    def get_reply_header(self):
        if self.repertoire_colour == "black":
            return "Ваш ответ (чёрные)"
        if self.repertoire_colour == "white":
            return "Ваш ответ (белые)"
        return "Ответ соперника"

    def show_results(self):
        if not self.results:
            self.lbl_status.setText("Совпадений не найдено")
            self.stats_text.setText("В файле не найдено ни одной партии с таким ходом.")
            return

        reply_header = self.get_reply_header()
        self.table.setHorizontalHeaderLabels([
            "Партия", "№ хода", "Сторона", "Найденный ход", reply_header, "Оценка (cp)"
        ])

        self.table.setRowCount(len(self.results))
        for row, res in enumerate(self.results):
            self.table.setItem(row, 0, QTableWidgetItem(str(res["game"])))
            self.table.setItem(row, 1, QTableWidgetItem(str(res["move_number"])))
            self.table.setItem(row, 2, QTableWidgetItem(res["side"]))
            self.table.setItem(row, 3, QTableWidgetItem(res["played"]))
            self.table.setItem(row, 4, QTableWidgetItem(res["reply"]))
            self.table.setItem(row, 5, QTableWidgetItem("—"))

        total = len(self.results)
        self.lbl_status.setText(f"Найдено совпадений: {total}")

        replies = Counter(res["reply"] for res in self.results)
        move_numbers = Counter(res["move_number"] for res in self.results)

        text = f"ВСЕГО СОВПАДЕНИЙ: {total}\n\n"
        text += "НА КАКИХ ХОДАХ ВСТРЕЧАЕТСЯ:\n"
        for number, count in sorted(move_numbers.items()):
            text += f"  Ход №{number}: {count} раз ({count/total*100:.1f}%)\n"
        text += f"\n{reply_header.upper()}:\n"
        for reply, count in replies.most_common():
            text += f"  {reply:<20} {count:>5} раз ({count/total*100:.1f}%)\n"
        self.stats_text.setText(text)

        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def analyze_positions(self):
        if not self.results:
            return
        self.lbl_status.setText("Анализ движком...")
        self.table.setEnabled(False)

        self.analysis_thread = EngineAnalysisThread(
            self.results, self.engine_path, depth=12, multipv=3
        )
        self.analysis_thread.finished.connect(self.on_analysis_finished)
        self.analysis_thread.error.connect(self.on_analysis_error)
        self.analysis_thread.start()

    def on_analysis_finished(self, data):
        self.analysis_data = data
        self.table.setEnabled(True)
        self.lbl_status.setText(f"Анализ завершён. Найдено: {len(self.results)}")

        for row, res in enumerate(self.results):
            if row in data:
                score = data[row]["score"]
                self.table.setItem(row, 5, QTableWidgetItem(f"{score:+d}"))

        self.recommend_move()

    def on_analysis_error(self, msg):
        self.table.setEnabled(True)
        self.lbl_status.setText("Ошибка анализа")
        QMessageBox.warning(self, "Движок", msg)

    def recommend_move(self):
        if not self.results or not self.analysis_data:
            return

        THRESHOLD = 50

        # Пул кандидатов: объединяем все ходы, предложенные движком хоть где-то
        pool = {}
        for idx, data in self.analysis_data.items():
            for cand in data["candidates"]:
                san = cand["san"]
                if san not in pool:
                    pool[san] = {"uci": cand["uci"], "positions": {}}
                pool[san]["positions"][idx] = cand["score"]

        total_positions = len(self.analysis_data)
        if total_positions == 0 or not pool:
            return

        scored = []
        for san, info in pool.items():
            uci = info["uci"]
            legal_count = 0
            acceptable_count = 0
            total_loss = 0.0
            loss_count = 0
            missing_positions = []

            for idx, res in enumerate(self.results):
                if idx not in self.analysis_data:
                    continue
                board = chess.Board(res["fen"])
                try:
                    move = chess.Move.from_uci(uci)
                    if move not in board.legal_moves:
                        missing_positions.append(idx)
                        continue
                except Exception:
                    missing_positions.append(idx)
                    continue

                legal_count += 1
                best_score = self.analysis_data[idx]["score"]
                cand_score = info["positions"].get(idx)

                if cand_score is None:
                    loss = THRESHOLD + 1
                else:
                    loss = max(0, best_score - cand_score)

                total_loss += loss
                loss_count += 1
                if loss <= THRESHOLD:
                    acceptable_count += 1

            scored.append({
                "san": san,
                "uci": uci,
                "legal": legal_count,
                "acceptable": acceptable_count,
                "avg_loss": total_loss / loss_count if loss_count else 9999,
                "missing": len(missing_positions),
                "missing_idxs": missing_positions,
                "total": total_positions
            })

        if not scored:
            return

        scored.sort(key=lambda x: (-x["acceptable"], x["avg_loss"]))
        best = scored[0]

        lines = ["\n\n★ АНАЛИЗ УНИФИКАЦИИ ОТВЕТОВ:\n"]
        lines.append(
            f"Всего позиций: {best['total']} | Порог потери: {THRESHOLD} cp\n"
        )
        lines.append(f"{'Ход':<10} {'Легален':<12} {'Приемлем':<12} {'Ср.потеря':<12}")
        lines.append("-" * 50)
        for item in scored[:10]:
            lines.append(
                f"{item['san']:<10} "
                f"{item['legal']:>3}/{item['total']:<8} "
                f"{item['acceptable']:>3}/{item['total']:<8} "
                f"{item['avg_loss']:>7.1f} cp"
            )

        lines.append("")
        if best["acceptable"] == best["total"] and best["legal"] == best["total"]:
            lines.append(
                f"✅ РЕКОМЕНДАЦИЯ: «{best['san']}» легален во всех позициях "
                f"и не хуже порога ни в одной. Средняя потеря: {best['avg_loss']:.1f} cp. "
                f"Можно играть везде."
            )
        elif best["legal"] == best["total"]:
            lines.append(
                f"⚠ РЕКОМЕНДАЦИЯ: «{best['san']}» легален везде, но в "
                f"{best['total'] - best['acceptable']} позициях хуже порога. "
                f"Средняя потеря: {best['avg_loss']:.1f} cp."
            )
        else:
            lines.append(
                f"⚠ РЕКОМЕНДАЦИЯ: «{best['san']}» легален в {best['legal']}/{best['total']} позициях, "
                f"приемлем в {best['acceptable']}/{best['total']}. "
                f"Средняя потеря: {best['avg_loss']:.1f} cp. "
                f"В {best['missing']} позициях ход невозможен — нужен запасной вариант."
            )

        # Запасной вариант для проблемных позиций
        if best["legal"] < best["total"] and best["missing_idxs"]:
            fallback_scored = []
            for san, info in pool.items():
                if san == best["san"]:
                    continue
                legal_in_problem = 0
                acceptable_in_problem = 0
                for idx in best["missing_idxs"]:
                    board = chess.Board(self.results[idx]["fen"])
                    try:
                        move = chess.Move.from_uci(info["uci"])
                        if move in board.legal_moves:
                            legal_in_problem += 1
                            best_score = self.analysis_data[idx]["score"]
                            cand_score = info["positions"].get(idx)
                            if cand_score is None:
                                loss = THRESHOLD + 1
                            else:
                                loss = max(0, best_score - cand_score)
                            if loss <= THRESHOLD:
                                acceptable_in_problem += 1
                    except Exception:
                        pass
                if legal_in_problem > 0:
                    fallback_scored.append((san, legal_in_problem, acceptable_in_problem))

            if fallback_scored:
                fallback_scored.sort(key=lambda x: (-x[2], -x[1]))
                fb_san, fb_legal, fb_acc = fallback_scored[0]
                lines.append(
                    f"\n🔁 Запасной вариант для проблемных позиций: «{fb_san}» "
                    f"(легален в {fb_legal}/{len(best['missing_idxs'])}, "
                    f"приемлем в {fb_acc}/{len(best['missing_idxs'])} из них)."
                )

        current = self.stats_text.toPlainText()
        self.stats_text.setText(current + "\n".join(lines))

    def on_table_select(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if row >= len(self.results):
            return

        res = self.results[row]
        self.current_fen = res["fen"]
        self.fen_edit.setText(self.current_fen)

        board = chess.Board(res["fen"])

        if self.repertoire_colour == "black":
            orientation = chess.BLACK
        else:
            orientation = chess.WHITE

        svg_data = chess.svg.board(
            board=board,
            size=480,
            lastmove=chess.Move.from_uci(res["lastmove"]),
            coordinates=True,
            orientation=orientation
        )
        self.svg_widget.load(svg_data.encode("utf-8"))

        self.lbl_position.setText(
            f"Партия №{res['game']} | Ход {res['move_number']} | {res['side']} | {res['played']}"
        )

    def copy_fen(self):
        if self.current_fen:
            QApplication.clipboard().setText(self.current_fen)
            self.lbl_status.setText("FEN скопирован в буфер обмена")
        else:
            self.lbl_status.setText("Нет FEN для копирования")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ChessPGNAnalyzerQt()
    window.show()
    sys.exit(app.exec_())