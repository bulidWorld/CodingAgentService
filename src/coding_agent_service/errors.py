"""任务输入校验相关异常。"""


class TaskInputError(ValueError):
    """入站任务参数缺失、类型错误或格式不合法时抛出。"""
