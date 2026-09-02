import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_strategy():
    path = ROOT / "hs300_trend_rotation_bigqmt.py"
    spec = importlib.util.spec_from_file_location("hs300_strategy_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.S = module.new_state()
    return module


class StrategyLogicTests(unittest.TestCase):
    def setUp(self):
        self.strategy = load_strategy()

    def test_order_remark_fits_qmt_limit(self):
        remark = self.strategy.make_remark("20260902", "600000.SH", "BUY", 100)
        self.assertLess(len(remark), 24)
        self.assertNotEqual(
            remark,
            self.strategy.make_remark("20260902", "600000.SH", "SELL", 100),
        )

    def test_exit_buffer_never_exceeds_position_cap(self):
        rows = [
            {"code": "{:06d}.SZ".format(index)}
            for index in range(1, 13)
        ]
        held = [row["code"] for row in rows[5:10]]
        targets = self.strategy.select_target_codes(rows, held)
        self.assertEqual(held, targets)
        self.assertEqual(self.strategy.MAX_POSITIONS, len(targets))

    def test_removed_constituent_remains_managed_until_sold(self):
        self.strategy.QMT_BACKTEST_MODE = True
        self.strategy.S["managed_codes"] = ["600000.SH"]
        positions = {"600000.SH": {"total": 100, "available": 100}}
        managed = self.strategy.managed_position_codes(positions, ["000001.SZ"])
        self.assertEqual(["600000.SH"], managed)

    def test_full_odd_lot_can_be_sold(self):
        self.assertEqual(150, self.strategy.sell_lot(150))
        self.assertEqual(50, self.strategy.sell_lot(50))

    def test_fallback_does_not_require_dunder_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "members.csv"
            path.write_text("code\n600000.SH\n000001.SZ\n", encoding="ascii")
            self.strategy.FALLBACK_CONSTITUENTS_PATH = str(path)
            self.strategy.__dict__.pop("__file__", None)
            self.assertEqual(
                ["600000.SH", "000001.SZ"],
                self.strategy.load_fallback_constituents(),
            )

    def test_backtest_mode_uses_bar_timestamp(self):
        class Context:
            barpos = 1

            @staticmethod
            def get_bar_timetag(_):
                return 1788278400000

        self.strategy.QMT_BACKTEST_MODE = True
        value = self.strategy.context_datetime(Context())
        self.assertIsInstance(value, datetime)
        self.assertEqual("20260902", value.strftime("%Y%m%d"))


if __name__ == "__main__":
    unittest.main()
