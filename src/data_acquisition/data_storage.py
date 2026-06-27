"""
data_storage.py
===============
Modul ini menyediakan kelas DataStorage yang bertanggung jawab untuk
menyimpan dan memuat data issue yang sudah diekstrak ke/dari berbagai
format file (CSV, JSON).

Fitur utama:
- Penyimpanan ke format CSV (untuk analisis dengan pandas/Excel)
- Penyimpanan ke format JSON Lines / JSON array (untuk portabilitas)
- Pembuatan direktori output secara otomatis
- Penggabungan (append) data dari beberapa sumber/repositori
- Pelaporan ringkasan data yang tersimpan

Penulis: [Nama Anda]
Tanggal: 2026
"""

import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ===========================================================================
# Konfigurasi Logger
# ===========================================================================
logger = logging.getLogger(__name__)


class DataStorage:
    """
    Mengelola penyimpanan dan pemuatan data issue ke/dari file lokal.

    Mendukung format CSV dan JSON dengan kemampuan append data
    dari beberapa repositori ke satu file output.

    Attributes:
        output_dir (Path): Direktori untuk menyimpan semua file output.
    """

    # Urutan kolom yang konsisten untuk file CSV
    CSV_COLUMNS = [
        "repo_owner", "repo_name", "repo_full_name",
        "issue_id", "issue_number", "issue_url",
        "title", "body", "comments_text", "full_text",
        "state", "state_reason", "is_locked", "lock_reason",
        "created_at", "updated_at", "closed_at",
        "age_days", "created_year", "created_month",
        "severity", "has_severity_label", "is_bug",
        "label_names_str", "label_count",
        "is_assigned", "assignee_count", "assignee_logins_str",
        "comment_count",
        "reaction_total", "reaction_plus1", "reaction_minus1",
        "reaction_laugh", "reaction_hooray", "reaction_confused",
        "reaction_heart", "reaction_rocket", "reaction_eyes",
        "author_login", "author_type",
        "title_length", "body_length", "has_body",
        "milestone_title", "milestone_state",
        "data_collected_at",
    ]

    def __init__(self, output_dir: str = "data/raw") -> None:
        """
        Inisialisasi DataStorage.

        Args:
            output_dir (str): Path ke direktori output. Direktori akan dibuat
                              secara otomatis jika belum ada.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("DataStorage diinisialisasi. Output dir: %s", self.output_dir.resolve())

    def save_to_csv(
        self,
        records: list[dict],
        filename: str,
        append: bool = False,
    ) -> Path:
        """
        Menyimpan daftar record issue ke file CSV.

        Args:
            records (list[dict]): Daftar record issue yang akan disimpan.
            filename (str): Nama file CSV (mis. "github_issues.csv").
            append (bool): Jika True dan file sudah ada, data akan ditambahkan
                           ke akhir file tanpa menimpa. Jika False, file baru
                           akan dibuat (menimpa file lama jika ada).

        Returns:
            Path: Path lengkap ke file CSV yang tersimpan.

        Raises:
            ValueError: Jika records kosong dan file belum ada.
        """
        if not records:
            logger.warning("Tidak ada record untuk disimpan ke CSV.")
            return self.output_dir / filename

        filepath = self.output_dir / filename
        file_exists = filepath.exists()
        mode = "a" if (append and file_exists) else "w"
        write_header = not (append and file_exists)

        with open(filepath, mode, newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=self.CSV_COLUMNS,
                extrasaction="ignore",  # Abaikan field yang tidak ada di CSV_COLUMNS
            )
            if write_header:
                writer.writeheader()

            for record in records:
                # Serialisasi field list menjadi string untuk CSV
                csv_record = record.copy()
                for key in ("label_names", "assignee_logins"):
                    if key in csv_record and isinstance(csv_record[key], list):
                        del csv_record[key]  # Gunakan versi _str yang sudah ada
                writer.writerow(csv_record)

        action = "Ditambahkan ke" if (append and file_exists) else "Disimpan ke"
        logger.info(
            "%s file CSV: %s (%d record)", action, filepath, len(records)
        )
        return filepath

    def save_to_json(
        self,
        records: list[dict],
        filename: str,
        append: bool = False,
        indent: int = 2,
    ) -> Path:
        """
        Menyimpan daftar record issue ke file JSON.

        Jika append=True, file JSON yang ada akan dimuat dan record baru
        ditambahkan ke dalamnya sebelum disimpan ulang.

        Args:
            records (list[dict]): Daftar record issue yang akan disimpan.
            filename (str): Nama file JSON (mis. "github_issues.json").
            append (bool): Jika True, gabungkan dengan data yang sudah ada.
            indent (int): Level indentasi untuk formatting JSON (0 = minified).

        Returns:
            Path: Path lengkap ke file JSON yang tersimpan.
        """
        if not records:
            logger.warning("Tidak ada record untuk disimpan ke JSON.")
            return self.output_dir / filename

        filepath = self.output_dir / filename

        # Jika append dan file sudah ada, muat data lama terlebih dahulu
        existing_records = []
        if append and filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    existing_records = json.load(f)
                logger.info(
                    "Memuat %d record yang sudah ada dari %s",
                    len(existing_records), filepath,
                )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Gagal memuat JSON yang ada: %s. File akan ditimpa.", e)
                existing_records = []

        all_records = existing_records + records

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(all_records, f, ensure_ascii=False, indent=indent, default=str)

        logger.info(
            "Disimpan ke file JSON: %s (%d total record)", filepath, len(all_records)
        )
        return filepath

    def save_summary(
        self,
        records: list[dict],
        run_metadata: Optional[dict] = None,
    ) -> Path:
        """
        Menghasilkan dan menyimpan file ringkasan (summary) dari data yang
        dikumpulkan dalam format JSON yang mudah dibaca.

        Args:
            records (list[dict]): Seluruh record issue yang telah dikumpulkan.
            run_metadata (Optional[dict]): Metadata tambahan tentang proses
                                           pengambilan data (mis. waktu mulai/selesai).

        Returns:
            Path: Path ke file summary yang tersimpan.
        """
        if not records:
            logger.warning("Tidak ada record untuk diringkas.")
            return self.output_dir / "summary.json"

        # Hitung distribusi severity
        severity_dist: dict = {}
        for r in records:
            sev = r.get("severity") or "Unknown"
            severity_dist[sev] = severity_dist.get(sev, 0) + 1

        # Hitung distribusi per repositori
        repo_dist: dict = {}
        for r in records:
            repo = r.get("repo_full_name", "unknown")
            repo_dist[repo] = repo_dist.get(repo, 0) + 1

        # Hitung distribusi state
        state_dist: dict = {}
        for r in records:
            state = r.get("state", "unknown")
            state_dist[state] = state_dist.get(state, 0) + 1

        summary = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "total_records": len(records),
            "records_with_severity_label": sum(
                1 for r in records if r.get("has_severity_label")
            ),
            "records_is_bug": sum(1 for r in records if r.get("is_bug")),
            "distribution_by_severity": severity_dist,
            "distribution_by_repository": repo_dist,
            "distribution_by_state": state_dist,
            "has_body_count": sum(1 for r in records if r.get("has_body")),
            "is_assigned_count": sum(1 for r in records if r.get("is_assigned")),
            "avg_comment_count": round(
                sum(r.get("comment_count", 0) for r in records) / len(records), 2
            ),
        }

        if run_metadata:
            summary["run_metadata"] = run_metadata

        filepath = self.output_dir / "collection_summary.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        logger.info("Summary tersimpan ke: %s", filepath)
        return filepath
