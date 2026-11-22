"""高校就业服务：岗位持续采集、技能归类与培养跟进。

数据均为合成数据。用法见 README.md。
"""
from .service import CareerService
from .store import CareerStore, load_store, new_store, save_store

__version__ = "0.1.0"

__all__ = [
    "CareerService",
    "CareerStore",
    "load_store",
    "new_store",
    "save_store",
    "__version__",
]
