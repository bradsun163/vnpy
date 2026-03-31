"""
无界面启动脚本 —— 用于服务器/后台运行 CTA 策略

严格遵循官方 examples/no_ui/run.py 模式。
所有策略通过 cta_engine 的 API 加载和启动。
"""
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine

from vnpy_ctp import CtpGateway
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_riskmanager import RiskManagerApp
from vnpy_paperaccount import PaperAccountApp
from vnpy_datarecorder import DataRecorderApp


def main():
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(CtpGateway)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(RiskManagerApp)
    main_engine.add_app(PaperAccountApp)
    main_engine.add_app(DataRecorderApp)

    # ---------- 连接 CTP ----------
    # 配置从 ~/.vntrader/connect_ctp.json 自动加载
    # 或在此指定 setting dict
    ctp_setting = {
        # "用户名": "",
        # "密码": "",
        # "经纪商代码": "",
        # "交易服务器": "",
        # "行情服务器": "",
        # "产品名称": "",
        # "授权编码": "",
    }
    # main_engine.connect(ctp_setting, "CTP")

    # ---------- CTA 策略引擎 ----------
    cta_engine = main_engine.get_engine("CtaStrategy")
    # cta_engine.init_engine()
    # cta_engine.init_all_strategies()
    # cta_engine.start_all_strategies()

    print("无界面模式已启动，按 Ctrl+C 退出")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在关闭...")
    finally:
        main_engine.close()


if __name__ == "__main__":
    main()
