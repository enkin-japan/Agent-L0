"""ppt 配置段加载（仓库根 config.yaml → PPT_CFG）。寻址统一走 config_loader。"""
from ..utils.config_loader import load_section

PPT_CFG = load_section("ppt")
