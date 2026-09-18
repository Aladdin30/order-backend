"""QR Generator Service: Cryptographic signing, high-res vector/raster rendering, and batch ZIP archiving."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from typing import Sequence

import qrcode
import qrcode.constants
import qrcode.image.svg
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.auth import Table
from app.schemas.qr_export import (
    BatchQRExportManifest,
    QRExportFormat,
)

logger = logging.getLogger("app.services.qr_generator_service")


class QRGeneratorService:
    """Enterprise Cryptographic QR code generation, rendering, and batch archiving service."""

    # -----------------------------------------------------------------------
    # 1. Cryptographic Signing & Verification Engine
    # -----------------------------------------------------------------------

    @classmethod
    def get_client_domain(cls) -> str:
        """Retrieve client frontend domain for QR scan routing."""
        return getattr(settings, "CLIENT_DOMAIN", "app.restaurant.com")

    @classmethod
    def compute_signature(cls, table_id: uuid.UUID | str, branch_id: uuid.UUID | str, timestamp: int | str) -> str:
        """Compute HMAC-SHA256 signature for table and branch at given timestamp with normalized payload."""
        canonical_table = str(uuid.UUID(str(table_id)))
        canonical_branch = str(uuid.UUID(str(branch_id)))
        canonical_ts = int(timestamp)
        payload = f"{canonical_table}:{canonical_branch}:{canonical_ts}"
        secret = settings.SECRET_KEY.encode("utf-8")
        return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def create_signed_payload(
        cls,
        table_id: uuid.UUID,
        branch_id: uuid.UUID,
        timestamp: int | None = None,
    ) -> tuple[str, str, int, str]:
        """Generate deterministic signed payload and full client URL.

        Returns:
            tuple: (signed_url, signature, timestamp, raw_payload)
        """
        if timestamp is None:
            timestamp = int(datetime.now(timezone.utc).timestamp())

        sig = cls.compute_signature(table_id, branch_id, timestamp)
        domain = cls.get_client_domain()
        signed_url = f"https://{domain}/t/{table_id}?b={branch_id}&ts={timestamp}&sig={sig}"
        payload = f"{table_id}:{branch_id}:{timestamp}"
        return signed_url, sig, timestamp, payload

    @classmethod
    def verify_signed_url(
        cls,
        table_id: uuid.UUID,
        branch_id: uuid.UUID,
        timestamp: int,
        signature: str,
    ) -> bool:
        """Verify HMAC-SHA256 signature in constant time against timing attacks."""
        expected_sig = cls.compute_signature(table_id, branch_id, timestamp)
        return hmac.compare_digest(expected_sig, signature)

    # -----------------------------------------------------------------------
    # 2. Vector (SVG) & Raster (PNG) QR Asset Renderer
    # -----------------------------------------------------------------------

    @classmethod
    def render_table_qr(
        cls,
        signed_url: str,
        table_number: str,
        export_format: QRExportFormat = QRExportFormat.PNG,
        scale: int = 10,
        include_label: bool = True,
    ) -> tuple[bytes, str]:
        """Render single QR code asset with Error Correction H (30%) and optional label banner.

        Returns:
            tuple: (raw_bytes, media_type)
        """
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=scale,
            border=4,
        )
        qr.add_data(signed_url)
        qr.make(fit=True)

        if export_format == QRExportFormat.SVG:
            return cls._render_svg(qr, table_number, include_label)
        else:
            return cls._render_png(qr, table_number, include_label)

    @classmethod
    def _render_svg(
        cls,
        qr: qrcode.QRCode,
        table_number: str,
        include_label: bool,
    ) -> tuple[bytes, str]:
        """Render vector SVG QR asset with injected label text."""
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        xml_str = buf.getvalue().decode("utf-8")

        if include_label:
            ET.register_namespace("", "http://www.w3.org/2000/svg")
            root = ET.fromstring(xml_str)
            vb = root.attrib.get("viewBox", "").split()
            if len(vb) == 4:
                x, y, w, h = float(vb[0]), float(vb[1]), float(vb[2]), float(vb[3])
                label_h = h * 0.18
                new_h = h + label_h
                root.attrib["viewBox"] = f"{x} {y} {w} {new_h}"

                if "height" in root.attrib and root.attrib["height"].endswith("mm"):
                    orig_mm = float(root.attrib["height"].replace("mm", ""))
                    root.attrib["height"] = f"{orig_mm * (new_h / h):.1f}mm"

                text_el = ET.SubElement(root, "{http://www.w3.org/2000/svg}text")
                text_el.attrib["x"] = f"{w / 2:.2f}"
                text_el.attrib["y"] = f"{h + label_h * 0.72:.2f}"
                text_el.attrib["text-anchor"] = "middle"
                text_el.attrib["font-family"] = "sans-serif, Arial, Helvetica"
                text_el.attrib["font-size"] = f"{label_h * 0.48:.2f}"
                text_el.attrib["font-weight"] = "bold"
                text_el.attrib["fill"] = "#000000"
                text_el.text = f"Table {table_number}"

            xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            return xml_bytes, "image/svg+xml"

        return buf.getvalue(), "image/svg+xml"

    @classmethod
    def _render_png(
        cls,
        qr: qrcode.QRCode,
        table_number: str,
        include_label: bool,
    ) -> tuple[bytes, str]:
        """Render raster PNG QR asset with drawn label banner."""
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        if include_label:
            w, h = img_qr.size
            banner_h = max(int(h * 0.16), 40)
            canvas = Image.new("RGB", (w, h + banner_h), "white")
            canvas.paste(img_qr, (0, 0))

            draw = ImageDraw.Draw(canvas)
            label_text = f"Table {table_number}"

            font_size = max(int(banner_h * 0.45), 14)
            try:
                font = ImageFont.load_default(size=font_size)
            except TypeError:
                font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), label_text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            text_x = (w - text_w) / 2
            text_y = h + (banner_h - text_h) / 2

            draw.text((text_x, text_y), label_text, fill="black", font=font)

            buf = io.BytesIO()
            canvas.save(buf, format="PNG")
            return buf.getvalue(), "image/png"

        buf = io.BytesIO()
        img_qr.save(buf, format="PNG")
        return buf.getvalue(), "image/png"

    # -----------------------------------------------------------------------
    # 3. Batch ZIP Packaging (Zero Disk Footprint)
    # -----------------------------------------------------------------------

    @classmethod
    def sanitize_filename(cls, table_number: str, export_format: QRExportFormat, pad: bool = True) -> str:
        """Construct standard filename for table asset."""
        clean_num = str(table_number).strip()
        if clean_num.isdigit():
            if pad:
                return f"table_{int(clean_num):02d}.{export_format.value}"
            return f"table_{clean_num}.{export_format.value}"
        # Replace spaces and slashes with underscores
        safe_name = clean_num.replace(" ", "_").replace("/", "-")
        return f"table_{safe_name}.{export_format.value}"

    @classmethod
    async def export_branch_qr_batch(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
        table_ids: list[uuid.UUID] | None = None,
        export_format: QRExportFormat = QRExportFormat.PNG,
        scale: int = 10,
        include_label: bool = True,
    ) -> tuple[bytes, str]:
        """Fetch active branch tables in a single query, render assets, and package in-memory ZIP.

        Returns:
            tuple: (zip_bytes, zip_filename)
        """
        # Fetch active tables strictly scoped to the branch ordered by table_number
        stmt = (
            select(Table)
            .where(
                Table.branch_id == branch_id,
                Table.is_active.is_(True),
            )
            .order_by(Table.table_number)
        )
        if table_ids:
            stmt = stmt.where(Table.id.in_(table_ids))

        result = await db.execute(stmt)
        tables: Sequence[Table] = result.scalars().all()

        zip_buffer = io.BytesIO()
        file_names: list[str] = []
        now = datetime.now(timezone.utc)

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for table in tables:
                signed_url, _, _, _ = cls.create_signed_payload(table.id, branch_id)
                img_bytes, _ = cls.render_table_qr(
                    signed_url=signed_url,
                    table_number=table.table_number,
                    export_format=export_format,
                    scale=scale,
                    include_label=include_label,
                )
                filename = cls.sanitize_filename(table.table_number, export_format)
                zip_file.writestr(filename, img_bytes)
                file_names.append(filename)

            # Build and embed manifest.json
            manifest = BatchQRExportManifest(
                branch_id=branch_id,
                total_tables=len(file_names),
                format=export_format,
                files=file_names,
                generated_at=now,
            )
            manifest_json = manifest.model_dump_json(indent=2)
            zip_file.writestr("manifest.json", manifest_json)

        zip_buffer.seek(0)
        zip_filename = f"branch_{branch_id}_qr_pack.zip"
        return zip_buffer.getvalue(), zip_filename
