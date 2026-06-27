"""
github_client.py
================
Modul ini menyediakan kelas GitHubAPIClient yang bertanggung jawab untuk
semua komunikasi dengan GitHub REST API v3.

Fitur utama:
- Autentikasi menggunakan Personal Access Token (PAT)
- Penanganan rate limit secara otomatis (menunggu hingga reset)
- Mekanisme retry dengan exponential backoff untuk error jaringan sementara
- Paginasi otomatis menggunakan header 'Link' dari respons GitHub
- Logging terstruktur untuk pemantauan proses

Penulis: [Nama Anda]
Tanggal: 2026
"""

import logging
import time
from typing import Any, Generator, Optional
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===========================================================================
# Konfigurasi Logger
# ===========================================================================
logger = logging.getLogger(__name__)


class RateLimitExceededError(Exception):
    """
    Exception kustom yang dilempar ketika rate limit GitHub API terlampaui
    dan waktu tunggu melebihi batas yang ditentukan.
    """
    pass


class GitHubAPIClient:
    """
    Klien untuk berinteraksi dengan GitHub REST API v3.

    Kelas ini menangani autentikasi PAT, paginasi otomatis,
    penanganan rate limit, dan mekanisme retry untuk permintaan HTTP.

    Attributes:
        BASE_URL (str): URL dasar GitHub REST API.
        DEFAULT_PER_PAGE (int): Jumlah item per halaman (maksimum GitHub: 100).
        MAX_WAIT_SECONDS (int): Batas maksimum waktu tunggu rate limit (detik).

    Contoh penggunaan:
        >>> client = GitHubAPIClient(token="ghp_xxxxxxxx")
        >>> for issue in client.paginate("/repos/microsoft/vscode/issues"):
        ...     print(issue["title"])
    """

    BASE_URL: str = "https://api.github.com"
    DEFAULT_PER_PAGE: int = 100
    MAX_WAIT_SECONDS: int = 3600  # Maksimum 1 jam menunggu reset rate limit

    def __init__(
        self,
        token: str,
        per_page: int = DEFAULT_PER_PAGE,
        max_retries: int = 5,
        backoff_factor: float = 1.5,
    ) -> None:
        """
        Inisialisasi GitHubAPIClient.

        Args:
            token (str): GitHub Personal Access Token (PAT) untuk autentikasi.
            per_page (int): Jumlah item yang diminta per halaman API.
                            Nilai maksimum yang diizinkan GitHub adalah 100.
            max_retries (int): Jumlah maksimum percobaan ulang untuk
                                error HTTP sementara (mis. 5xx, koneksi terputus).
            backoff_factor (float): Faktor pengali untuk exponential backoff
                                    antar percobaan ulang.

        Raises:
            ValueError: Jika token kosong atau per_page di luar rentang 1-100.
        """
        if not token or not token.strip():
            raise ValueError("Token GitHub PAT tidak boleh kosong.")
        if not (1 <= per_page <= 100):
            raise ValueError("Nilai per_page harus antara 1 hingga 100.")

        self._token = token.strip()
        self.per_page = per_page
        self._session = self._build_session(max_retries, backoff_factor)
        logger.info("GitHubAPIClient berhasil diinisialisasi.")

    # -----------------------------------------------------------------------
    # Metode Privat (Helper Internal)
    # -----------------------------------------------------------------------

    def _build_session(self, max_retries: int, backoff_factor: float) -> requests.Session:
        """
        Membuat dan mengonfigurasi objek requests.Session dengan:
        - Header autentikasi Bearer Token.
        - Header Accept untuk menggunakan format JSON standar GitHub.
        - HTTPAdapter dengan retry strategy untuk error sementara.

        Args:
            max_retries (int): Jumlah maksimum percobaan ulang.
            backoff_factor (float): Faktor backoff untuk jeda antar retry.

        Returns:
            requests.Session: Objek sesi HTTP yang sudah dikonfigurasi.
        """
        session = requests.Session()

        # Header standar untuk GitHub REST API v3
        session.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

        # Konfigurasi retry otomatis untuk error jaringan dan server
        # Status 429 = Too Many Requests, 500/502/503/504 = Server Error
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def _handle_rate_limit(self, response: requests.Response) -> None:
        """
        Memeriksa header respons untuk mendeteksi rate limit dan
        menunggu secara otomatis hingga batas rate limit direset.

        GitHub memiliki dua jenis rate limit:
        1. Primary rate limit (header: X-RateLimit-Remaining)
        2. Secondary rate limit (header: Retry-After)

        Args:
            response (requests.Response): Objek respons HTTP dari GitHub.

        Raises:
            RateLimitExceededError: Jika waktu tunggu melebihi MAX_WAIT_SECONDS.
        """
        # Tangani secondary rate limit (Retry-After header)
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            wait_seconds = int(retry_after) + 5  # Tambah buffer 5 detik
            if wait_seconds > self.MAX_WAIT_SECONDS:
                raise RateLimitExceededError(
                    f"Retry-After ({wait_seconds}s) melebihi batas maksimum "
                    f"({self.MAX_WAIT_SECONDS}s)."
                )
            logger.warning(
                "Secondary rate limit terdeteksi. Menunggu %d detik...", wait_seconds
            )
            time.sleep(wait_seconds)
            return

        # Tangani primary rate limit (X-RateLimit-Remaining = 0)
        remaining = response.headers.get("X-RateLimit-Remaining", "1")
        if remaining == "0" or response.status_code == 403:
            reset_timestamp = response.headers.get("X-RateLimit-Reset")
            if reset_timestamp:
                reset_time = int(reset_timestamp)
                current_time = int(time.time())
                wait_seconds = max(0, reset_time - current_time) + 10  # buffer 10 detik

                if wait_seconds > self.MAX_WAIT_SECONDS:
                    raise RateLimitExceededError(
                        f"Waktu tunggu reset rate limit ({wait_seconds}s) melebihi "
                        f"batas maksimum ({self.MAX_WAIT_SECONDS}s)."
                    )

                logger.warning(
                    "Primary rate limit tercapai. Menunggu %d detik hingga reset...",
                    wait_seconds,
                )
                time.sleep(wait_seconds)

    def _get_next_page_url(self, response: requests.Response) -> Optional[str]:
        """
        Mengekstrak URL halaman berikutnya dari header 'Link' pada respons GitHub.

        Header 'Link' memiliki format seperti:
        <https://api.github.com/...?page=2>; rel="next",
        <https://api.github.com/...?page=5>; rel="last"

        Args:
            response (requests.Response): Objek respons HTTP dari GitHub.

        Returns:
            Optional[str]: URL halaman berikutnya, atau None jika sudah halaman terakhir.
        """
        link_header = response.headers.get("Link", "")
        if not link_header:
            return None

        # Parsing setiap bagian dari header Link
        for part in link_header.split(","):
            section = part.strip().split(";")
            if len(section) == 2:
                url_part = section[0].strip().strip("<>")
                rel_part = section[1].strip()
                if rel_part == 'rel="next"':
                    return url_part

        return None

    # -----------------------------------------------------------------------
    # Metode Publik (Antarmuka Utama)
    # -----------------------------------------------------------------------

    def get(self, endpoint: str, params: Optional[dict] = None) -> Any:
        """
        Mengirim permintaan GET ke endpoint GitHub API dan mengembalikan
        data JSON dari respons.

        Args:
            endpoint (str): Path endpoint API (mis. "/repos/owner/repo/issues")
                            atau URL lengkap (mis. "https://api.github.com/...").
            params (Optional[dict]): Parameter query string tambahan.

        Returns:
            Any: Data yang di-parse dari respons JSON (bisa berupa dict atau list).

        Raises:
            requests.HTTPError: Jika respons mengembalikan status error yang tidak dapat dipulihkan.
            RateLimitExceededError: Jika rate limit melebihi batas tunggu maksimum.
        """
        # Bangun URL lengkap jika hanya path endpoint yang diberikan
        url = endpoint if endpoint.startswith("http") else f"{self.BASE_URL}{endpoint}"

        logger.debug("GET %s | params: %s", url, params)

        response = self._session.get(url, params=params, timeout=30)

        # Tangani rate limit sebelum memproses respons
        if response.status_code in (403, 429):
            self._handle_rate_limit(response)
            # Coba ulang setelah menunggu
            response = self._session.get(url, params=params, timeout=30)

        # Lempar exception untuk error HTTP yang tidak dapat dipulihkan
        response.raise_for_status()

        return response.json()

    def paginate(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        max_items: int = 0,
    ) -> Generator[dict, None, None]:
        """
        Generator yang mengiterasi semua halaman dari endpoint GitHub API
        dan menghasilkan (yield) setiap item satu per satu.

        Paginasi dilakukan secara otomatis dengan mengikuti header 'Link'
        pada setiap respons hingga tidak ada halaman berikutnya.

        Args:
            endpoint (str): Path endpoint API atau URL lengkap.
            params (Optional[dict]): Parameter query string tambahan.
                                     Catatan: 'per_page' akan ditambahkan otomatis.
            max_items (int): Batas maksimum item yang dihasilkan.
                              Nilai 0 berarti tidak ada batas (ambil semua).

        Yields:
            dict: Satu item data dari respons API (mis. satu objek issue).

        Raises:
            requests.HTTPError: Untuk error HTTP yang tidak dapat dipulihkan.
            RateLimitExceededError: Jika rate limit melebihi batas waktu tunggu.
        """
        request_params = params.copy() if params else {}
        request_params.setdefault("per_page", self.per_page)

        url: Optional[str] = (
            endpoint if endpoint.startswith("http") else f"{self.BASE_URL}{endpoint}"
        )

        total_yielded = 0
        page_number = 1

        while url:
            logger.info(
                "Mengambil halaman %d dari: %s",
                page_number,
                url.split("?")[0],  # Log URL tanpa query string agar lebih bersih
            )

            response = self._session.get(url, params=request_params, timeout=30)

            # Tangani rate limit
            if response.status_code in (403, 429):
                self._handle_rate_limit(response)
                response = self._session.get(url, params=request_params, timeout=30)

            response.raise_for_status()

            items = response.json()

            # Pastikan respons berupa list
            if not isinstance(items, list):
                logger.warning(
                    "Respons dari %s bukan list, tipe: %s. Menghentikan paginasi.",
                    url,
                    type(items).__name__,
                )
                break

            if not items:
                logger.info("Halaman kosong diterima. Paginasi selesai.")
                break

            for item in items:
                yield item
                total_yielded += 1

                # Hentikan jika sudah mencapai batas maksimum item
                if max_items > 0 and total_yielded >= max_items:
                    logger.info(
                        "Batas maksimum item (%d) tercapai. Menghentikan paginasi.",
                        max_items,
                    )
                    return

            # Ambil URL halaman berikutnya dari header Link
            url = self._get_next_page_url(response)

            # Setelah halaman pertama, hapus params agar tidak duplikat dengan URL paginasi
            request_params = {}
            page_number += 1

        logger.info("Paginasi selesai. Total item dihasilkan: %d", total_yielded)

    def check_authentication(self) -> dict:
        """
        Memverifikasi bahwa token PAT valid dengan memanggil endpoint /user.

        Returns:
            dict: Data profil pengguna GitHub yang terautentikasi.

        Raises:
            requests.HTTPError: Jika autentikasi gagal (mis. token tidak valid).
        """
        logger.info("Memverifikasi autentikasi token GitHub PAT...")
        user_data = self.get("/user")
        logger.info(
            "Autentikasi berhasil! Login sebagai: %s (ID: %s)",
            user_data.get("login"),
            user_data.get("id"),
        )
        return user_data

    def get_rate_limit_status(self) -> dict:
        """
        Mengambil status rate limit saat ini dari GitHub API.

        Returns:
            dict: Informasi rate limit termasuk sisa kuota dan waktu reset.
        """
        data = self.get("/rate_limit")
        core = data.get("resources", {}).get("core", {})
        logger.info(
            "Rate Limit — Limit: %s | Remaining: %s | Reset: %s",
            core.get("limit"),
            core.get("remaining"),
            time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(core.get("reset", 0)),
            ),
        )
        return data
