import asyncio
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp


ALBUM_ID_RE = re.compile(r"(?:jm)?\s*(\d{3,})", re.IGNORECASE)


class JMComicPlugin(Star):
    """根据 JMComic 本子编号下载并发送文件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.download_root = Path(config.get("download_root", "data/jmcomic")).expanduser()
        self.album_root = self.download_root / "albums"
        self.archive_root = self.download_root / "archives"
        self.client_impl = str(config.get("client_impl", "api") or "api")
        self.proxy = str(config.get("proxy", "system") or "system")
        self.image_suffix = str(config.get("image_suffix", ".jpg") or ".jpg")
        self.output_format = str(config.get("output_format", "pdf") or "pdf").lower()
        self.upload_filename_mode = str(
            config.get("upload_filename_mode", "random") or "random"
        ).lower()
        self.upload_filename_prefix = str(
            config.get("upload_filename_prefix", "document") or "document"
        )
        self.max_file_mb = int(config.get("max_file_mb", 100) or 100)
        self.remove_images_after_pack = bool(
            config.get(
                "remove_images_after_pack",
                config.get("remove_images_after_zip", True),
            )
        )
        self._locks: dict[str, asyncio.Lock] = {}

        self.album_root.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)

    @filter.command("jm", alias={"jmcomic", "\u672c\u5b50"})
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def download_jm_album(self, event: AstrMessageEvent, album: str = ""):
        """用法：/jm 123456"""
        album_id = self._extract_album_id(album or event.message_str)
        if not album_id:
            yield event.plain_result("请提供本子编号，例如：/jm 123456")
            return

        event.stop_event()
        await event.send(
            MessageChain(
                [
                    Comp.Plain(
                        f"已收到 JM{album_id}，正在服务端下载并打包文件，请稍等..."
                    )
                ]
            )
        )

        lock = self._locks.setdefault(album_id, asyncio.Lock())
        async with lock:
            try:
                archive = await asyncio.to_thread(self._download_and_pack, album_id)
            except ModuleNotFoundError as exc:
                missing = getattr(exc, "name", "unknown")
                logger.error(f"加载 jmcomic 时缺少 Python 模块：{missing}")
                await event.send(
                    MessageChain(
                        [
                            Comp.Plain(
                                f"缺少 Python 模块：{missing}。"
                                "请安装本插件 requirements.txt 中的依赖。"
                            )
                        ]
                    )
                )
                return
            except Exception as exc:
                logger.exception(f"下载 JM{album_id} 失败：{exc}")
                await event.send(MessageChain([Comp.Plain(f"下载 JM{album_id} 失败：{exc}")]))
                return

        size_mb = archive.stat().st_size / 1024 / 1024
        if self.max_file_mb > 0 and size_mb > self.max_file_mb:
            await event.send(
                MessageChain(
                    [
                        Comp.Plain(
                            f"JM{album_id} 已打包完成，但文件大小为 {size_mb:.1f} MB，"
                            f"超过当前配置限制 {self.max_file_mb} MB，未发送。"
                            f"本地文件路径：{archive}"
                        )
                    ]
                )
            )
            return

        await event.send(MessageChain([Comp.Plain(f"JM{album_id} 已打包完成，正在上传文件...")]))
        await self._send_onebot_file(event, archive)

    def _extract_album_id(self, text: str) -> str | None:
        match = ALBUM_ID_RE.search(text or "")
        return match.group(1) if match else None

    def _download_and_pack(self, album_id: str) -> Path:
        self._ensure_astrbot_site_packages()
        import jmcomic

        work_dir = self.album_root / album_id
        output_format = self.output_format if self.output_format in {"pdf", "zip"} else "pdf"
        archive = self.archive_root / f"JM{album_id}.{output_format}"

        if archive.exists() and archive.stat().st_size > 0:
            return archive

        work_dir.mkdir(parents=True, exist_ok=True)
        option_path = work_dir / "option.yml"
        option_path.write_text(self._build_option_yaml(work_dir), encoding="utf-8")

        option = jmcomic.create_option_by_file(str(option_path))
        jmcomic.download_album(album_id, option)

        files = [
            path
            for path in work_dir.rglob("*")
            if path.is_file() and path.name != "option.yml"
        ]
        if not files:
            raise RuntimeError("下载完成，但没有找到图片文件。")

        image_files = [path for path in files if self._is_image_file(path)]
        if not image_files:
            raise RuntimeError("下载完成，但没有找到图片文件。")
        image_files = sorted(image_files, key=self._natural_path_key)

        if output_format == "pdf":
            self._make_pdf(image_files, archive)
        else:
            self._make_zip(files, archive, work_dir)

        if self.remove_images_after_pack:
            self._cleanup_album_dir(work_dir, option_path)

        return archive

    def _make_zip(self, files: list[Path], archive: Path, work_dir: Path) -> None:
        tmp_archive = archive.with_suffix(".zip.tmp")
        if tmp_archive.exists():
            tmp_archive.unlink()

        with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in files:
                zf.write(file_path, file_path.relative_to(work_dir))

        tmp_archive.replace(archive)

    def _make_pdf(self, image_files: list[Path], archive: Path) -> None:
        from PIL import Image, ImageOps

        tmp_archive = archive.with_suffix(".pdf.tmp")
        if tmp_archive.exists():
            tmp_archive.unlink()

        pages = []
        try:
            for image_file in image_files:
                with Image.open(image_file) as img:
                    img = ImageOps.exif_transpose(img)
                    if img.mode in ("RGBA", "LA") or (
                        img.mode == "P" and "transparency" in img.info
                    ):
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                        pages.append(background)
                    else:
                        pages.append(img.convert("RGB"))

            if not pages:
                raise RuntimeError("没有生成任何 PDF 页面。")

            first, rest = pages[0], pages[1:]
            first.save(tmp_archive, "PDF", save_all=True, append_images=rest)
            tmp_archive.replace(archive)
        finally:
            for page in pages:
                page.close()

    def _cleanup_album_dir(self, work_dir: Path, option_path: Path) -> None:
        for child in work_dir.iterdir():
            if child == option_path:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            elif child.is_file():
                child.unlink(missing_ok=True)

    def _is_image_file(self, path: Path) -> bool:
        return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    def _natural_path_key(self, path: Path) -> list[Any]:
        parts = []
        for part in path.parts:
            for token in re.split(r"(\d+)", part.lower()):
                if token.isdigit():
                    parts.append(int(token))
                elif token:
                    parts.append(token)
        return parts

    def _ensure_astrbot_site_packages(self) -> None:
        candidates = []
        if path := os.environ.get("ASTRBOT_ROOT"):
            candidates.append(Path(path) / "data" / "site-packages")
        candidates.append(Path.home() / ".astrbot" / "data" / "site-packages")

        for candidate in candidates:
            if not candidate.is_dir():
                continue
            site_packages = str(candidate.resolve())
            if site_packages not in sys.path:
                sys.path.insert(0, site_packages)

    def _build_option_yaml(self, base_dir: Path) -> str:
        extra_yaml = str(self.config.get("extra_option_yaml", "") or "").strip()
        lines = [
            "log: true",
            "client:",
            f"  impl: {self.client_impl}",
            "  postman:",
            "    meta_data:",
            f"      proxies: {self._yaml_scalar(self.proxy)}",
            "download:",
            "  cache: true",
            "  image:",
            "    decode: true",
            f"    suffix: {self.image_suffix}",
            "  threading:",
            "    image: 10",
            "    photo: 4",
            "dir_rule:",
            f"  base_dir: {self._yaml_scalar(str(base_dir.resolve()))}",
            "  rule: Bd / Aid / Pindex",
        ]
        if extra_yaml:
            lines.extend(["", "# 用户追加的 jmcomic option 配置", extra_yaml])
        return "\n".join(lines) + "\n"

    def _yaml_scalar(self, value: Any) -> str:
        text = str(value)
        if text.lower() in {"null", "true", "false"}:
            return text.lower()
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    async def _send_onebot_file(self, event: AstrMessageEvent, archive: Path) -> None:
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        file_path = str(archive.resolve())
        upload_name = self._make_upload_filename(archive)

        if bot is not None and group_id:
            await bot.call_action(
                "upload_group_file",
                group_id=int(group_id),
                file=file_path,
                name=upload_name,
            )
            return

        if bot is not None and sender_id:
            await bot.call_action(
                "upload_private_file",
                user_id=int(sender_id),
                file=file_path,
                name=upload_name,
            )
            return

        await event.send(MessageChain([Comp.File(file=file_path, name=upload_name)]))

    def _make_upload_filename(self, archive: Path) -> str:
        mode = self.upload_filename_mode
        suffix = archive.suffix or ".pdf"

        if mode == "original":
            return archive.name

        prefix = self._safe_filename_prefix(self.upload_filename_prefix)
        if mode == "timestamp":
            return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"

        random_id = uuid.uuid4().hex[:10]
        return f"{prefix}_{random_id}{suffix}"

    def _safe_filename_prefix(self, value: str) -> str:
        prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("._-")
        return prefix or "document"

    async def terminate(self):
        """插件卸载时调用。"""
        self._locks.clear()
