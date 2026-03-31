"""
VeighNa Trader 启动脚本 —— 国内期货 CTA 工作流

严格遵循官方 examples/veighna_trader/run.py 模式：
  MainEngine → add_gateway → add_app → MainWindow

所有策略文件放在 ~/.vntrader/ 目录下的 strategies 文件夹中，
通过 CTA 策略引擎的 GUI 界面加载和管理。
"""
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

# Gateway —— 国内期货 CTP 接口
from vnpy_ctp import CtpGateway

# App —— CTA 完整工作流
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_datamanager import DataManagerApp
from vnpy_datarecorder import DataRecorderApp
from vnpy_riskmanager import RiskManagerApp
from vnpy_paperaccount import PaperAccountApp


def main():
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 添加 CTP 接口
    main_engine.add_gateway(CtpGateway)

    # 添加应用模块（按工作流顺序）
    main_engine.add_app(CtaStrategyApp)        # CTA 策略引擎
    main_engine.add_app(CtaBacktesterApp)       # CTA 回测引擎
    main_engine.add_app(DataManagerApp)         # 数据管理
    main_engine.add_app(DataRecorderApp)        # 行情录制
    main_engine.add_app(RiskManagerApp)         # 风控管理
    main_engine.add_app(PaperAccountApp)        # 模拟交易

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
