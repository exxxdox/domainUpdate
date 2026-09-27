from domain_update.service import DomainUpdateService


def update_record() -> bool:
    """保留命令行入口，与网页和定时任务复用同一业务流程。"""
    result = DomainUpdateService().check_and_update(source="cli")
    print(result.message)
    return result.ok


if __name__ == "__main__":
    raise SystemExit(0 if update_record() else 1)
