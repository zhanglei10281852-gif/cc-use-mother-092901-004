# 车端软件灰度发布控制

本项目定义车辆快照、发布波次和安装回执的基础对象，供车端软件分批发布、风险判断、暂停与回滚流程使用。

运行测试：\`python -m unittest discover -s tests -v\`

编译检查：\`python -m compileall -q src tests run_cli.py\`

命令行冒烟：\`python run_cli.py\`
