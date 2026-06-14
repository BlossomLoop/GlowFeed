"""管理 token 解析逻辑：命令行 --token > config.json 的 admin_token > 自动生成持久化。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.server import _resolve_token


class TestResolveToken(unittest.TestCase):
    def _cfg(self) -> str:
        """返回临时目录下的 config.json 文件路径（字符串）。"""
        return str(Path(tempfile.mkdtemp()) / "config.json")

    def test_cli_token_takes_priority(self):
        cfg = self._cfg()
        self.assertEqual(_resolve_token("cli-tok", cfg), "cli-tok")
        self.assertFalse(Path(cfg).exists())  # 命令行 token 不落盘

    def test_reads_admin_token_from_config(self):
        cfg = self._cfg()
        Path(cfg).write_text(json.dumps({"admin_token": "cfg-tok"}), encoding="utf-8")
        self.assertEqual(_resolve_token(None, cfg), "cfg-tok")

    def test_generates_and_persists_when_absent(self):
        cfg = self._cfg()
        t1 = _resolve_token(None, cfg)
        self.assertTrue(t1)
        self.assertTrue(Path(cfg).exists())
        saved = json.loads(Path(cfg).read_text(encoding="utf-8"))
        self.assertEqual(saved["admin_token"], t1)        # 已持久化
        self.assertEqual(_resolve_token(None, cfg), t1)   # 重启读到同一个，固定不变

    def test_empty_config_token_regenerates(self):
        cfg = self._cfg()
        Path(cfg).write_text(json.dumps({"admin_token": "   "}), encoding="utf-8")
        t = _resolve_token(None, cfg)
        self.assertTrue(t.strip())                        # 空白视为未配置，生成新的

    def test_corrupt_config_falls_back_to_generate(self):
        cfg = self._cfg()
        Path(cfg).write_text("{ not valid json", encoding="utf-8")
        t = _resolve_token(None, cfg)
        self.assertTrue(t)                                # 坏文件不崩，照样生成

    def test_cli_token_preserves_existing_config(self):
        cfg = self._cfg()
        Path(cfg).write_text(json.dumps({"admin_token": "cfg-tok"}), encoding="utf-8")
        self.assertEqual(_resolve_token("cli-tok", cfg), "cli-tok")  # 命令行优先
        saved = json.loads(Path(cfg).read_text(encoding="utf-8"))
        self.assertEqual(saved["admin_token"], "cfg-tok")           # 不应改写已有配置

    def test_generate_keeps_other_config_keys(self):
        """自动生成 token 时，必须保留 config.json 里的 server / llm 等其它配置。"""
        cfg = self._cfg()
        Path(cfg).write_text(json.dumps({
            "server": {"host": "0.0.0.0", "port": 9000},
            "llm": {"provider": "openai", "api_key": "sk-keep-me"},
        }), encoding="utf-8")
        t = _resolve_token(None, cfg)
        saved = json.loads(Path(cfg).read_text(encoding="utf-8"))
        self.assertEqual(saved["admin_token"], t)                   # 新 token 写入
        self.assertEqual(saved["server"], {"host": "0.0.0.0", "port": 9000})  # server 保留
        self.assertEqual(saved["llm"]["api_key"], "sk-keep-me")     # llm/api_key 保留


if __name__ == "__main__":
    unittest.main(verbosity=2)
