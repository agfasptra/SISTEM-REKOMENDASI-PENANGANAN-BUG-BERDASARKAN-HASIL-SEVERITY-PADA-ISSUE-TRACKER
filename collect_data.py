"""
collect_data.py
===============
Script utama (entry point) untuk Tahap 1: Akuisisi Data GitHub API.

Script ini mengorkestrasi seluruh proses pengumpulan data:
1. Memuat konfigurasi dari file .env
2. Memverifikasi autentikasi token PAT
3. Mengiterasi daftar repositori target
4. Mengekstrak issue lengkap (dengan komentar, label, assignee, dll.)
5. Menyimpan hasil ke format CSV dan JSON
6. Menghasilkan laporan ringkasan

Cara menjalankan:
    python collect_data.py

Konfigurasi melalui file .env (salin dari .env.example):
    GITHUB_TOKEN=ghp_xxxxxxxx
    GITHUB_REPOS=owner1/repo1,owner2/repo2
    MAX_ISSUES_PER_REPO=500
    ISSUE_STATE=all

Penulis: [Nama Anda]
Tanggal: 2026
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Tambahkan root direktori proyek ke sys.path agar import relatif berfungsi
# saat script dijalankan langsung (bukan sebagai modul)
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import modul proyek
from src.data_acquisition.github_client import GitHubAPIClient, RateLimitExceededError
from src.data_acquisition.issue_extractor import IssueExtractor
from src.data_acquisition.data_storage import DataStorage

# ===========================================================================
# Setup Logging
# ===========================================================================

def setup_logging(log_level: str = "INFO", log_to_file: bool = True) -> None:
    """
    Mengonfigurasi sistem logging untuk menampilkan output ke konsol
    dan (opsional) menyimpan ke file log.

    Args:
        log_level (str): Level logging — DEBUG, INFO, WARNING, ERROR, CRITICAL.
        log_to_file (bool): Jika True, log juga disimpan ke file di 'logs/'.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]

    if log_to_file:
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"data_collection_{timestamp}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        handlers.append(file_handler)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

logger = logging.getLogger(__name__)


# ===========================================================================
# Fungsi Helper Konfigurasi
# ===========================================================================

def load_env_config() -> dict:
    """
    Memuat konfigurasi dari environment variables atau file .env.

    Mendukung pembacaan otomatis dari file .env menggunakan python-dotenv
    (jika tersedia), atau membaca langsung dari environment OS.

    Returns:
        dict: Konfigurasi yang sudah divalidasi berisi:
            - 'token' (str): GitHub PAT.
            - 'repos' (list[str]): Daftar repositori target.
            - 'max_issues' (int): Batas maksimum issue per repo.
            - 'issue_state' (str): Filter state issue.

    Raises:
        SystemExit: Jika konfigurasi wajib tidak ditemukan.
    """
    # Coba load python-dotenv jika tersedia
    try:
        from dotenv import load_dotenv
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.info("File .env berhasil dimuat dari: %s", env_path)
        else:
            logger.warning(
                "File .env tidak ditemukan di %s. Menggunakan environment OS.", env_path
            )
    except ImportError:
        logger.warning(
            "python-dotenv tidak terinstal. Membaca langsung dari environment OS. "
            "Instal dengan: pip install python-dotenv"
        )

    # Baca konfigurasi wajib
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        logger.critical(
            "GITHUB_TOKEN tidak ditemukan! Harap set environment variable atau buat file .env"
        )
        sys.exit(1)

    repos_raw = os.environ.get("GITHUB_REPOS", "").strip()
    if not repos_raw:
        logger.critical(
            "GITHUB_REPOS tidak ditemukan! Harap set daftar repositori (format: owner/repo)."
        )
        sys.exit(1)

    # Parse daftar repositori
    repos = [r.strip() for r in repos_raw.split(",") if r.strip()]
    invalid_repos = [r for r in repos if "/" not in r or len(r.split("/")) != 2]
    if invalid_repos:
        logger.critical(
            "Format repositori tidak valid: %s. Gunakan format 'owner/repo'.", invalid_repos
        )
        sys.exit(1)

    # Baca konfigurasi opsional
    try:
        max_issues = int(os.environ.get("MAX_ISSUES_PER_REPO", "0"))
    except ValueError:
        logger.warning("MAX_ISSUES_PER_REPO tidak valid, menggunakan default: 0 (semua)")
        max_issues = 0

    issue_state = os.environ.get("ISSUE_STATE", "all").lower()
    if issue_state not in ("open", "closed", "all"):
        logger.warning("ISSUE_STATE tidak valid ('%s'), menggunakan 'all'.", issue_state)
        issue_state = "all"

    config = {
        "token": token,
        "repos": repos,
        "max_issues": max_issues,
        "issue_state": issue_state,
    }

    logger.info(
        "Konfigurasi dimuat: %d repositori target, state='%s', max_issues=%d",
        len(repos), issue_state, max_issues,
    )
    return config


def parse_repo_string(repo_string: str) -> tuple[str, str]:
    """
    Memisahkan string 'owner/repo' menjadi tuple (owner, repo).

    Args:
        repo_string (str): String repositori dalam format 'owner/repo'.

    Returns:
        tuple[str, str]: Tuple berisi (owner, repo_name).

    Raises:
        ValueError: Jika format tidak sesuai.
    """
    parts = repo_string.strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Format repositori tidak valid: '{repo_string}'. Gunakan 'owner/repo'.")
    return parts[0].strip(), parts[1].strip()


# ===========================================================================
# Fungsi Utama Orkestrasi
# ===========================================================================

def run_data_collection(config: dict) -> list[dict]:
    """
    Mengeksekusi seluruh pipeline pengumpulan data GitHub Issue.

    Proses:
    1. Inisialisasi klien API dan verifikasi autentikasi
    2. Cek status rate limit awal
    3. Iterasi setiap repositori target
    4. Ekstraksi issue dan penyimpanan per repositori
    5. Simpan semua data ke file konsolidasi

    Args:
        config (dict): Konfigurasi yang sudah divalidasi dari load_env_config().

    Returns:
        list[dict]: Seluruh record issue dari semua repositori.
    """
    start_time = datetime.now(timezone.utc)
    logger.info("=" * 70)
    logger.info("MULAI PROSES AKUISISI DATA GITHUB ISSUES")
    logger.info("Waktu mulai: %s", start_time.strftime("%Y-%m-%d %H:%M:%S UTC"))
    logger.info("=" * 70)

    # --- Inisialisasi Komponen ---
    client = GitHubAPIClient(token=config["token"])
    extractor = IssueExtractor(client=client, fetch_comments=True, max_comments_per_issue=30)
    storage = DataStorage(output_dir="data/raw")

    # --- Verifikasi Autentikasi ---
    try:
        user_info = client.check_authentication()
        logger.info(
            "✓ Terautentikasi sebagai: @%s (Name: %s)",
            user_info.get("login"),
            user_info.get("name", "N/A"),
        )
    except Exception as e:
        logger.critical("✗ Gagal autentikasi: %s", e)
        sys.exit(1)

    # --- Cek Status Rate Limit Awal ---
    try:
        client.get_rate_limit_status()
    except Exception as e:
        logger.warning("Tidak dapat mengambil status rate limit: %s", e)

    # --- Proses Setiap Repositori ---
    all_records: list[dict] = []
    repo_results: list[dict] = []

    for idx, repo_string in enumerate(config["repos"], start=1):
        logger.info(
            "\n[%d/%d] Memproses repositori: %s",
            idx, len(config["repos"]), repo_string,
        )
        logger.info("-" * 50)

        try:
            owner, repo_name = parse_repo_string(repo_string)
        except ValueError as e:
            logger.error("Melewati repositori karena format tidak valid: %s", e)
            continue

        repo_start = time.time()

        try:
            records = extractor.extract_issues_from_repo(
                owner=owner,
                repo=repo_name,
                state=config["issue_state"],
                max_issues=config["max_issues"],
            )

            repo_elapsed = time.time() - repo_start

            if records:
                # Simpan data per repositori (file terpisah)
                safe_repo_name = repo_string.replace("/", "_")
                storage.save_to_csv(records, filename=f"{safe_repo_name}_issues.csv")
                storage.save_to_json(records, filename=f"{safe_repo_name}_issues.json")

                # Tambahkan ke akumulasi semua repositori
                all_records.extend(records)

                repo_result = {
                    "repo": repo_string,
                    "status": "success",
                    "total_issues": len(records),
                    "with_severity": sum(1 for r in records if r.get("has_severity_label")),
                    "bugs_only": sum(1 for r in records if r.get("is_bug")),
                    "elapsed_seconds": round(repo_elapsed, 2),
                }
            else:
                logger.warning("Tidak ada issue yang diekstrak dari %s", repo_string)
                repo_result = {
                    "repo": repo_string,
                    "status": "empty",
                    "total_issues": 0,
                    "elapsed_seconds": round(repo_elapsed, 2),
                }

        except RateLimitExceededError as e:
            logger.error(
                "Rate limit melebihi batas untuk %s: %s. Melewati repositori ini.", repo_string, e
            )
            repo_result = {"repo": repo_string, "status": "rate_limit_error", "error": str(e)}

        except Exception as e:
            logger.error("Error tidak terduga saat memproses %s: %s", repo_string, e, exc_info=True)
            repo_result = {"repo": repo_string, "status": "error", "error": str(e)}

        repo_results.append(repo_result)

    # --- Simpan Data Konsolidasi (Semua Repositori) ---
    if all_records:
        logger.info("\n" + "=" * 70)
        logger.info("Menyimpan data konsolidasi dari semua repositori...")

        storage.save_to_csv(all_records, filename="all_github_issues.csv")
        storage.save_to_json(all_records, filename="all_github_issues.json")

        end_time = datetime.now(timezone.utc)
        run_metadata = {
            "github_user": user_info.get("login"),
            "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "total_duration_seconds": round((end_time - start_time).total_seconds(), 2),
            "repositories_processed": repo_results,
            "config": {
                "issue_state": config["issue_state"],
                "max_issues_per_repo": config["max_issues"],
            },
        }
        storage.save_summary(all_records, run_metadata=run_metadata)

    # --- Laporan Akhir ---
    end_time = datetime.now(timezone.utc)
    total_duration = (end_time - start_time).total_seconds()

    logger.info("\n" + "=" * 70)
    logger.info("PROSES AKUISISI DATA SELESAI")
    logger.info("=" * 70)
    logger.info("Waktu selesai  : %s", end_time.strftime("%Y-%m-%d %H:%M:%S UTC"))
    logger.info("Durasi total   : %.2f detik (%.1f menit)", total_duration, total_duration / 60)
    logger.info("Total issue    : %d dari %d repositori", len(all_records), len(config["repos"]))
    logger.info("-" * 70)

    for result in repo_results:
        status_icon = "✓" if result["status"] == "success" else "✗"
        logger.info(
            "  %s %-40s | %s",
            status_icon,
            result["repo"],
            result.get("status", "unknown"),
        )
        if result["status"] == "success":
            logger.info(
                "    → Issues: %d | Severity labeled: %d | Bug labeled: %d",
                result.get("total_issues", 0),
                result.get("with_severity", 0),
                result.get("bugs_only", 0),
            )

    logger.info("=" * 70)

    return all_records


# ===========================================================================
# Entry Point
# ===========================================================================

if __name__ == "__main__":
    # Setup logging
    setup_logging(log_level="INFO", log_to_file=True)

    logger.info("Sistem Rekomendasi Penanganan Bug — Tahap 1: Akuisisi Data")
    logger.info("Python version: %s", sys.version)

    # Muat konfigurasi
    config = load_env_config()

    # Jalankan proses pengumpulan data
    try:
        all_records = run_data_collection(config)
        sys.exit(0)
    except KeyboardInterrupt:
        logger.warning("\nProses dihentikan oleh pengguna (Ctrl+C).")
        sys.exit(130)
    except Exception as e:
        logger.critical("Error fatal yang tidak tertangani: %s", e, exc_info=True)
        sys.exit(1)
