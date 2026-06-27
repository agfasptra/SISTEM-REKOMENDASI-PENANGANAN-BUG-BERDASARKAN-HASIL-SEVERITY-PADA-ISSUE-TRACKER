"""
issue_extractor.py
==================
Modul ini menyediakan kelas IssueExtractor yang bertanggung jawab untuk
mengekstrak, mentransformasi, dan mengumpulkan data issue dari GitHub
ke dalam format terstruktur yang siap untuk pipeline ML.

Atribut yang diekstrak mencakup:
- Metadata inti issue (id, nomor, judul, deskripsi, status, tanggal)
- Label dan severity (dari label yang ada)
- Informasi penugasan (assignee)
- Komentar (jumlah + konten teks gabungan)
- Reaksi dan keterlibatan komunitas
- Metadata repositori asal

Penulis: [Nama Anda]
Tanggal: 2026
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.data_acquisition.github_client import GitHubAPIClient

# ===========================================================================
# Konfigurasi Logger
# ===========================================================================
logger = logging.getLogger(__name__)

# ===========================================================================
# Konstanta Pemetaan Severity
# ===========================================================================
# Pemetaan dari nama label GitHub ke tingkat severity yang digunakan sistem.
# Anda dapat menyesuaikan daftar ini sesuai dengan label di repositori target.
SEVERITY_LABEL_MAP: dict[str, str] = {
    # Critical
    "critical": "Critical",
    "severity: critical": "Critical",
    "sev1": "Critical",
    "p0": "Critical",
    "blocker": "Critical",
    # High
    "high": "High",
    "severity: high": "High",
    "sev2": "High",
    "p1": "High",
    "major": "High",
    # Medium
    "medium": "Medium",
    "severity: medium": "Medium",
    "sev3": "Medium",
    "p2": "Medium",
    "moderate": "Medium",
    # Low
    "low": "Low",
    "severity: low": "Low",
    "sev4": "Low",
    "p3": "Low",
    "minor": "Low",
    "trivial": "Low",
}

# Label yang mengindikasikan issue adalah laporan bug
BUG_LABELS: set[str] = {
    "bug", "defect", "error", "fault", "fix", "problem",
    "regression", "type: bug", "kind: bug", "category: bug",
}


class IssueExtractor:
    """
    Mengekstrak dan menstransformasi data issue dari GitHub API
    menjadi format record yang terstruktur dan kaya fitur.

    Kelas ini berkolaborasi dengan GitHubAPIClient untuk mengambil
    data issue, komentar, dan reaksi, lalu menggabungkannya menjadi
    satu record flat yang mudah diproses oleh pipeline ML.

    Attributes:
        client (GitHubAPIClient): Klien API GitHub yang sudah terotentikasi.
        fetch_comments (bool): Apakah konten komentar diambil atau hanya jumlahnya.
        max_comments_per_issue (int): Batas maksimum komentar yang diambil per issue.
    """

    def __init__(
        self,
        client: GitHubAPIClient,
        fetch_comments: bool = True,
        max_comments_per_issue: int = 50,
    ) -> None:
        """
        Inisialisasi IssueExtractor.

        Args:
            client (GitHubAPIClient): Instance klien API GitHub.
            fetch_comments (bool): Jika True, konten teks komentar akan diambil
                                   dan digabungkan. Menonaktifkan ini akan
                                   mempercepat akuisisi data secara signifikan.
            max_comments_per_issue (int): Batas maksimum komentar yang diambil
                                          per issue untuk menghindari penggunaan
                                          API yang berlebihan pada issue populer.
        """
        self.client = client
        self.fetch_comments = fetch_comments
        self.max_comments_per_issue = max_comments_per_issue
        logger.info(
            "IssueExtractor diinisialisasi. fetch_comments=%s, max_comments=%d",
            fetch_comments,
            max_comments_per_issue,
        )

    # -----------------------------------------------------------------------
    # Metode Privat (Transformasi Data)
    # -----------------------------------------------------------------------

    def _parse_datetime(self, dt_string: Optional[str]) -> Optional[str]:
        """
        Mengonversi string datetime format ISO 8601 dari GitHub ke format
        yang lebih mudah dibaca (YYYY-MM-DD HH:MM:SS UTC).

        Args:
            dt_string (Optional[str]): String datetime dari GitHub API,
                                       mis. "2023-01-15T10:30:00Z".

        Returns:
            Optional[str]: String datetime yang sudah diformat, atau None
                           jika input None atau format tidak valid.
        """
        if not dt_string:
            return None
        try:
            dt = datetime.fromisoformat(dt_string.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except (ValueError, AttributeError):
            logger.warning("Format datetime tidak valid: %s", dt_string)
            return dt_string

    def _calculate_age_days(
        self,
        created_at: Optional[str],
        closed_at: Optional[str] = None,
    ) -> Optional[float]:
        """
        Menghitung umur issue dalam hari (dari dibuat hingga ditutup atau sekarang).

        Args:
            created_at (Optional[str]): Tanggal pembuatan issue (ISO 8601).
            closed_at (Optional[str]): Tanggal penutupan issue (ISO 8601),
                                       atau None jika issue masih terbuka.

        Returns:
            Optional[float]: Umur issue dalam hari (dibulatkan 2 desimal),
                             atau None jika created_at tidak tersedia.
        """
        if not created_at:
            return None
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            end_time = (
                datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                if closed_at
                else datetime.now(timezone.utc)
            )
            delta = end_time - created
            return round(delta.total_seconds() / 86400, 2)
        except (ValueError, AttributeError):
            return None

    def _extract_labels(self, raw_labels: list) -> dict:
        """
        Mengekstrak informasi label dari data mentah GitHub API dan
        menentukan tingkat severity berdasarkan pemetaan SEVERITY_LABEL_MAP.

        Args:
            raw_labels (list): Daftar objek label dari GitHub API response.

        Returns:
            dict: Dictionary berisi:
                - 'label_names' (list[str]): Semua nama label.
                - 'label_count' (int): Jumlah total label.
                - 'severity' (Optional[str]): Tingkat severity yang terdeteksi,
                                              atau None jika tidak ditemukan.
                - 'is_bug' (bool): True jika issue memiliki label bug.
                - 'has_severity_label' (bool): True jika label severity ditemukan.
        """
        label_names = [lbl.get("name", "").strip() for lbl in raw_labels if lbl.get("name")]
        label_names_lower = [name.lower() for name in label_names]

        # Deteksi severity dari label
        severity = None
        for label_lower in label_names_lower:
            if label_lower in SEVERITY_LABEL_MAP:
                severity = SEVERITY_LABEL_MAP[label_lower]
                break  # Ambil severity pertama yang cocok

        # Deteksi apakah issue adalah bug
        is_bug = any(lbl in BUG_LABELS for lbl in label_names_lower)

        return {
            "label_names": label_names,
            "label_count": len(label_names),
            "severity": severity,
            "is_bug": is_bug,
            "has_severity_label": severity is not None,
        }

    def _extract_assignee_info(self, raw_issue: dict) -> dict:
        """
        Mengekstrak informasi assignee dari data mentah issue GitHub.

        GitHub mendukung satu 'assignee' (primary) dan beberapa 'assignees'.
        Metode ini menggabungkan keduanya.

        Args:
            raw_issue (dict): Data mentah issue dari GitHub API.

        Returns:
            dict: Dictionary berisi:
                - 'is_assigned' (bool): True jika issue sudah memiliki assignee.
                - 'assignee_count' (int): Jumlah total assignee.
                - 'assignee_logins' (list[str]): Daftar username assignee.
        """
        assignees = raw_issue.get("assignees") or []
        primary_assignee = raw_issue.get("assignee")

        # Gabungkan assignee utama dengan daftar assignees
        all_logins = {a.get("login") for a in assignees if a.get("login")}
        if primary_assignee and primary_assignee.get("login"):
            all_logins.add(primary_assignee["login"])

        logins_list = sorted(list(all_logins))
        return {
            "is_assigned": len(logins_list) > 0,
            "assignee_count": len(logins_list),
            "assignee_logins": logins_list,
        }

    def _fetch_comments_text(
        self, owner: str, repo: str, issue_number: int, comment_count: int
    ) -> str:
        """
        Mengambil konten teks dari komentar sebuah issue dan
        menggabungkannya menjadi satu string.

        Args:
            owner (str): Username pemilik repositori.
            repo (str): Nama repositori.
            issue_number (int): Nomor issue di GitHub.
            comment_count (int): Jumlah komentar yang diketahui (untuk optimasi).

        Returns:
            str: Semua teks komentar digabungkan dengan separator newline.
                 Mengembalikan string kosong jika tidak ada komentar atau
                 fetch_comments dinonaktifkan.
        """
        if not self.fetch_comments or comment_count == 0:
            return ""

        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        comments_text_parts = []

        try:
            for comment in self.client.paginate(
                endpoint,
                params={"per_page": 100},
                max_items=self.max_comments_per_issue,
            ):
                body = comment.get("body") or ""
                if body.strip():
                    comments_text_parts.append(body.strip())
        except Exception as e:
            logger.warning(
                "Gagal mengambil komentar untuk issue #%d (%s/%s): %s",
                issue_number, owner, repo, e,
            )

        return "\n---\n".join(comments_text_parts)

    # -----------------------------------------------------------------------
    # Metode Publik (Antarmuka Utama)
    # -----------------------------------------------------------------------

    def extract_issue_record(
        self, raw_issue: dict, owner: str, repo: str
    ) -> Optional[dict]:
        """
        Mentransformasi satu objek issue mentah dari GitHub API menjadi
        record terstruktur yang kaya fitur untuk pipeline ML.

        Record yang dihasilkan berisi semua atribut yang diperlukan untuk
        preprocessing NLP, ekstraksi fitur TF-IDF, dan klasifikasi severity.

        Args:
            raw_issue (dict): Objek issue mentah dari GitHub REST API v3.
            owner (str): Username pemilik repositori.
            repo (str): Nama repositori.

        Returns:
            Optional[dict]: Record issue terstruktur, atau None jika issue
                            adalah Pull Request (bukan Issue murni).
        """
        # GitHub API /issues juga mengembalikan Pull Requests.
        # Kita filter dengan memeriksa keberadaan field 'pull_request'.
        if raw_issue.get("pull_request"):
            return None

        issue_number = raw_issue.get("number")

        # --- Ekstraksi Label ---
        label_info = self._extract_labels(raw_issue.get("labels") or [])

        # --- Ekstraksi Assignee ---
        assignee_info = self._extract_assignee_info(raw_issue)

        # --- Ekstraksi Komentar ---
        comment_count = raw_issue.get("comments", 0)
        comments_text = self._fetch_comments_text(owner, repo, issue_number, comment_count)

        # --- Parsing Timestamp ---
        created_at_raw = raw_issue.get("created_at")
        updated_at_raw = raw_issue.get("updated_at")
        closed_at_raw = raw_issue.get("closed_at")

        # --- Ekstraksi Reaksi ---
        reactions = raw_issue.get("reactions") or {}

        # --- Bangun Record Final ---
        record = {
            # ---- Identifikasi ----
            "repo_owner": owner,
            "repo_name": repo,
            "repo_full_name": f"{owner}/{repo}",
            "issue_id": raw_issue.get("id"),
            "issue_number": issue_number,
            "issue_url": raw_issue.get("html_url"),
            "api_url": raw_issue.get("url"),

            # ---- Konten Utama (Fitur NLP) ----
            "title": (raw_issue.get("title") or "").strip(),
            "body": (raw_issue.get("body") or "").strip(),
            "comments_text": comments_text,

            # Gabungan teks untuk TF-IDF (title + body + comments)
            "full_text": " ".join(filter(None, [
                (raw_issue.get("title") or "").strip(),
                (raw_issue.get("body") or "").strip(),
                comments_text,
            ])),

            # ---- Status & Lifecycle ----
            "state": raw_issue.get("state"),  # "open" atau "closed"
            "state_reason": raw_issue.get("state_reason"),  # "completed", "not_planned", dll.
            "is_locked": raw_issue.get("locked", False),
            "lock_reason": raw_issue.get("active_lock_reason"),

            # ---- Timestamps ----
            "created_at": self._parse_datetime(created_at_raw),
            "updated_at": self._parse_datetime(updated_at_raw),
            "closed_at": self._parse_datetime(closed_at_raw),
            "created_at_raw": created_at_raw,  # Format asli untuk perhitungan

            # ---- Fitur Turunan Waktu ----
            "age_days": self._calculate_age_days(created_at_raw, closed_at_raw),
            "created_year": (
                datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).year
                if created_at_raw else None
            ),
            "created_month": (
                datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).month
                if created_at_raw else None
            ),

            # ---- Label & Severity (Target Klasifikasi) ----
            "severity": label_info["severity"],
            "has_severity_label": label_info["has_severity_label"],
            "is_bug": label_info["is_bug"],
            "label_names": label_info["label_names"],
            "label_names_str": ", ".join(label_info["label_names"]),
            "label_count": label_info["label_count"],

            # ---- Assignee ----
            "is_assigned": assignee_info["is_assigned"],
            "assignee_count": assignee_info["assignee_count"],
            "assignee_logins": assignee_info["assignee_logins"],
            "assignee_logins_str": ", ".join(assignee_info["assignee_logins"]),

            # ---- Statistik Keterlibatan ----
            "comment_count": comment_count,
            "reaction_total": reactions.get("total_count", 0),
            "reaction_plus1": reactions.get("+1", 0),
            "reaction_minus1": reactions.get("-1", 0),
            "reaction_laugh": reactions.get("laugh", 0),
            "reaction_hooray": reactions.get("hooray", 0),
            "reaction_confused": reactions.get("confused", 0),
            "reaction_heart": reactions.get("heart", 0),
            "reaction_rocket": reactions.get("rocket", 0),
            "reaction_eyes": reactions.get("eyes", 0),

            # ---- Informasi Pembuat Issue ----
            "author_login": (raw_issue.get("user") or {}).get("login"),
            "author_type": (raw_issue.get("user") or {}).get("type"),  # "User" atau "Bot"

            # ---- Fitur Tekstual Turunan ----
            "title_length": len((raw_issue.get("title") or "")),
            "body_length": len((raw_issue.get("body") or "")),
            "has_body": bool((raw_issue.get("body") or "").strip()),

            # ---- Metadata Milestone ----
            "milestone_title": (raw_issue.get("milestone") or {}).get("title"),
            "milestone_state": (raw_issue.get("milestone") or {}).get("state"),

            # ---- Metadata Pengambilan Data ----
            "data_collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }

        return record

    def extract_issues_from_repo(
        self,
        owner: str,
        repo: str,
        state: str = "all",
        max_issues: int = 0,
        only_bugs: bool = False,
    ) -> list[dict]:
        """
        Mengambil dan mengekstrak semua issue dari sebuah repositori GitHub.

        Args:
            owner (str): Username pemilik repositori (mis. "microsoft").
            repo (str): Nama repositori (mis. "vscode").
            state (str): Filter status issue — "open", "closed", atau "all".
            max_issues (int): Batas maksimum issue yang diproses.
                              Nilai 0 berarti tidak ada batas.
            only_bugs (bool): Jika True, hanya issue dengan label bug yang disertakan.

        Returns:
            list[dict]: Daftar record issue yang sudah diekstrak dan distrukturkan.
        """
        logger.info(
            "Mulai ekstraksi issue dari repositori: %s/%s (state=%s, max=%d)",
            owner, repo, state, max_issues,
        )

        endpoint = f"/repos/{owner}/{repo}/issues"
        params = {
            "state": state,
            "sort": "created",
            "direction": "desc",
        }

        extracted_records = []
        raw_count = 0
        skipped_pr = 0
        skipped_no_bug = 0

        for raw_issue in self.client.paginate(endpoint, params=params, max_items=max_issues):
            raw_count += 1

            record = self.extract_issue_record(raw_issue, owner, repo)

            # Lewati Pull Requests
            if record is None:
                skipped_pr += 1
                continue

            # Filter hanya bug jika diminta
            if only_bugs and not record["is_bug"]:
                skipped_no_bug += 1
                continue

            extracted_records.append(record)

            # Log progress setiap 100 issue
            if len(extracted_records) % 100 == 0:
                logger.info(
                    "Progress %s/%s: %d issue berhasil diekstrak...",
                    owner, repo, len(extracted_records),
                )

        logger.info(
            "Ekstraksi selesai untuk %s/%s. "
            "Total raw: %d | Issue valid: %d | PR dilewati: %d | Non-bug dilewati: %d",
            owner, repo,
            raw_count,
            len(extracted_records),
            skipped_pr,
            skipped_no_bug,
        )

        return extracted_records
