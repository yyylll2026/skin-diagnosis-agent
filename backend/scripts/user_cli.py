"""用户管理 CLI。

## 为什么必须有这个脚本

本部署开启了**自助注册**，但注册接口把角色硬编码为 `customer`——
否则任何人都能把自己注册成管理员。所以管理员账号只能从这里创建：

```bash
cd backend
python -m scripts.user_cli create-admin --username root
python -m scripts.user_cli list
python -m scripts.user_cli reset-password --username alice
python -m scripts.user_cli set-role --username bob --role admin
```

密码默认**交互式输入**（不回显），也支持 `--password` 供脚本/容器编排使用——
但后者会把密码留在 shell 历史与进程列表里，只在自动化场景用。

## 与 Web 层的关系

这里直接调用 `AuthService`，因此**「不能删最后一个管理员」等约束同样生效**，
不会出现「CLI 绕过业务规则」的漏洞。审计日志也会带上 `actor=cli` 作为来源。
"""

from __future__ import annotations

import getpass
import sys

import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.core.errors import AppError
from app.repositories.db import MetadataStore
from app.schemas.auth import Role, UserStatus
from app.services.auth_service import AuthService

app = typer.Typer(
    help="皮肤诊断智能体 · 用户管理",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

CLI_ACTOR = "cli"


def _service() -> AuthService:
    """构造 CLI 用的鉴权服务。

    注意：这里**不读 `auth_enabled`**——账号管理属于数据操作，
    与「接口是否校验身份」无关。关掉鉴权时依然要能建号。
    """
    settings = get_settings()
    return AuthService(settings, MetadataStore(settings.resolved_database_url))


def _read_password(confirm: bool = True) -> str:
    """交互式读取密码并做二次确认。"""
    first = getpass.getpass("请输入密码：")
    if confirm:
        second = getpass.getpass("请再次输入密码：")
        if first != second:
            console.print("[red]两次输入的密码不一致[/red]")
            raise typer.Exit(code=1)
    return first


def _fail(message: str) -> None:
    """统一输出错误并退出。"""
    console.print(f"[red]ERROR[/red] {message}")
    raise typer.Exit(code=1)


@app.command("create-admin")
def create_admin(
    username: str = typer.Option(..., "--username", "-u", help="管理员用户名"),
    password: str = typer.Option("", "--password", "-p", help="密码；留空则交互式输入"),
    display_name: str = typer.Option("", "--name", help="显示名，默认同用户名"),
) -> None:
    """创建管理员账号（自助注册无法产生管理员，只能用这个命令）。"""
    service = _service()
    pwd = password or _read_password()
    try:
        user = service.create_user(
            username=AuthService.normalize_username(username),
            password=pwd,
            role=Role.ADMIN,
            display_name=display_name or username,
            actor=CLI_ACTOR,
        )
    except AppError as exc:
        _fail(exc.message)
        return
    console.print(f"[green]OK[/green] 已创建管理员 {user.username}（{user.user_id}）")


@app.command("create-user")
def create_user(
    username: str = typer.Option(..., "--username", "-u", help="用户名"),
    password: str = typer.Option("", "--password", "-p", help="密码；留空则交互式输入"),
    role: str = typer.Option("customer", "--role", "-r", help="角色：admin / customer"),
    display_name: str = typer.Option("", "--name", help="显示名，默认同用户名"),
) -> None:
    """创建用户（可指定角色）。"""
    try:
        parsed_role = Role(role.strip().lower())
    except ValueError:
        _fail(f"角色只能是 {[r.value for r in Role]}")
        return

    service = _service()
    pwd = password or _read_password()
    try:
        user = service.create_user(
            username=AuthService.normalize_username(username),
            password=pwd,
            role=parsed_role,
            display_name=display_name or username,
            actor=CLI_ACTOR,
        )
    except AppError as exc:
        _fail(exc.message)
        return
    console.print(f"[green]OK[/green] 已创建 {user.username}（{user.role}）")


@app.command("list")
def list_users(
    keyword: str = typer.Option("", "--keyword", "-k", help="按用户名或显示名搜索"),
) -> None:
    """列出全部用户。"""
    users = _service().list_users(keyword=keyword)
    if not users:
        console.print("（暂无用户。用 create-admin 创建第一个管理员）")
        return

    table = Table(title=f"用户（{len(users)}）", show_lines=False)
    table.add_column("用户名", style="cyan")
    table.add_column("显示名")
    table.add_column("角色", style="magenta")
    table.add_column("状态")
    table.add_column("创建时间")
    table.add_column("最后登录")
    for user in users:
        status_style = "green" if user.status == UserStatus.ACTIVE else "red"
        table.add_row(
            user.username,
            user.display_name,
            user.role.value,
            f"[{status_style}]{user.status.value}[/{status_style}]",
            user.created_at,
            user.last_login_at or "—",
        )
    console.print(table)


@app.command("set-role")
def set_role(
    username: str = typer.Option(..., "--username", "-u", help="用户名"),
    role: str = typer.Option(..., "--role", "-r", help="新角色：admin / customer"),
) -> None:
    """修改用户角色（受「必须保留至少一个管理员」约束）。"""
    try:
        parsed = Role(role.strip().lower())
    except ValueError:
        _fail(f"角色只能是 {[r.value for r in Role]}")
        return

    service = _service()
    target = _find_by_username(service, username)
    if target is None:
        return
    try:
        # 不传 actor：CLI 场景下没有「当前登录用户」，因此「不能降级自己」不适用，
        # 但「不能没有管理员」这条依然由服务层强制
        service.update_user(target.user_id, role=parsed, actor=None)
    except AppError as exc:
        _fail(exc.message)
        return
    console.print(f"[green]OK[/green] {target.username} 的角色已改为 {parsed.value}")


@app.command("set-status")
def set_status(
    username: str = typer.Option(..., "--username", "-u", help="用户名"),
    status: str = typer.Option(..., "--status", "-s", help="active / disabled"),
) -> None:
    """启用或禁用账号。禁用会立即踢掉该用户所有登录会话。"""
    try:
        parsed = UserStatus(status.strip().lower())
    except ValueError:
        _fail(f"状态只能是 {[s.value for s in UserStatus]}")
        return

    service = _service()
    target = _find_by_username(service, username)
    if target is None:
        return
    try:
        service.update_user(target.user_id, status=parsed, actor=None)
    except AppError as exc:
        _fail(exc.message)
        return
    console.print(f"[green]OK[/green] {target.username} 已设为 {parsed.value}")


@app.command("reset-password")
def reset_password(
    username: str = typer.Option(..., "--username", "-u", help="用户名"),
    password: str = typer.Option("", "--password", "-p", help="新密码；留空则交互式输入"),
) -> None:
    """重置密码，并踢掉该用户所有登录会话。"""
    service = _service()
    target = _find_by_username(service, username)
    if target is None:
        return
    pwd = password or _read_password()
    try:
        service.reset_password(target.user_id, pwd, actor=None)
    except AppError as exc:
        _fail(exc.message)
        return
    console.print(f"[green]OK[/green] {target.username} 的密码已重置，其所有会话已失效")


@app.command("delete")
def delete_user(
    username: str = typer.Option(..., "--username", "-u", help="用户名"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
) -> None:
    """删除用户（对话历史保留，便于审计追溯）。"""
    service = _service()
    target = _find_by_username(service, username)
    if target is None:
        return
    if not yes:
        confirmed = typer.confirm(f"确定要删除用户 {target.username} 吗？")
        if not confirmed:
            console.print("已取消")
            return
    try:
        service.delete_user(target.user_id, actor=None)
    except AppError as exc:
        _fail(exc.message)
        return
    console.print(f"[green]OK[/green] 已删除 {target.username}")


@app.command("audit")
def audit(
    limit: int = typer.Option(30, "--limit", "-n", help="显示条数"),
    action: str = typer.Option("", "--action", "-a", help="按动作过滤"),
) -> None:
    """查看审计日志。"""
    logs = _service().list_audit_logs(limit=limit, action=action)
    if not logs:
        console.print("（暂无记录）")
        return
    table = Table(title=f"审计日志（最近 {len(logs)} 条）")
    table.add_column("时间", style="dim")
    table.add_column("动作", style="cyan")
    table.add_column("操作者")
    table.add_column("对象")
    table.add_column("结果")
    table.add_column("IP", style="dim")
    for row in logs:
        result = str(row.get("result") or "ok")
        style = "green" if result == "ok" else "yellow"
        table.add_row(
            str(row.get("created_at") or ""),
            str(row.get("action") or ""),
            str(row.get("actor") or "—"),
            str(row.get("target") or "—"),
            f"[{style}]{result}[/{style}]",
            str(row.get("client_ip") or "—"),
        )
    console.print(table)


def _find_by_username(service: AuthService, username: str):
    """按用户名找用户，找不到则报错退出。"""
    from app.schemas.auth import UserInfo

    target: UserInfo | None = None
    target_name = AuthService.normalize_username(username)
    for item in service.list_users(keyword=target_name):
        if item.username == target_name:
            target = item
            break
    if target is None:
        _fail(f"用户不存在：{username}")
        return None
    return target


def main() -> None:
    """入口（供 `python -m scripts.user_cli` 调用）。"""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n已中断")
        sys.exit(130)


if __name__ == "__main__":
    main()
