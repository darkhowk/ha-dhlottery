# -*- coding: utf-8 -*-
"""
DH Lottery Pension 720+ Client
연금복권 720+ 구매 및 정보 조회

암호화: AES-128-CBC + PBKDF2(SHA256, 1000iter) / key = JSESSIONID[:32]
Base URL: https://el.dhlottery.co.kr
Purchase Flow: makeOrderNo.do → connPro.do → checkDeposit.do
"""

import datetime
import logging
import base64
import os
import random
import time
from dataclasses import dataclass
from typing import Optional, List
from urllib.parse import urlencode, parse_qs, quote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from yarl import URL

_LOGGER = logging.getLogger(__name__)

EL_BASE_URL = "https://el.dhlottery.co.kr"


class DhPension720Error(Exception):
    pass


class DhPension720PurchaseError(DhPension720Error):
    pass


@dataclass
class DhPension720BuyData:
    round_no: int
    ticket_count: int
    tickets: str
    fail_count: int
    fail_tickets: str
    amount: int


@dataclass
class PensionTicket:
    """연금복권 720+ 구매 티켓 (조 + 번호)"""
    group: int              # 조 (1~5)
    number: Optional[int] = None  # 6자리 번호 (0~999999), None = 자동(서버 선택)

    @property
    def is_auto(self) -> bool:
        return self.number is None

    def buy_no(self) -> str:
        """BUY_NO 형식: {조}{번호:06d}"""
        if self.number is None:
            return f"{self.group}000000"
        return f"{self.group}{self.number:06d}"

    def set_type(self) -> str:
        """BUY_SET_TYPE: SA=자동, SE=수동"""
        return "SA" if self.number is None else "SE"


@dataclass
class DhPension720BuyHistoryData:
    round_no: int
    issue_dt: str
    barcode: str
    ticket_count: int
    amount: int
    result: str


# ---------------------------------------------------------------------------
# AES encryption / decryption  (matches game.jsp encrypt() / decrypt())
# ---------------------------------------------------------------------------

def _encrypt(plaintext: str, jsessionid: str) -> str:
    passphrase = jsessionid[:32].encode("utf-8")
    salt = os.urandom(32)
    iv = os.urandom(16)

    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=16, salt=salt, iterations=1000
    ).derive(passphrase)

    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    raw = salt.hex() + iv.hex() + base64.b64encode(ciphertext).decode("ascii")
    return quote(raw, safe="")


def _decrypt(enc_text: str, jsessionid: str) -> str:
    passphrase = jsessionid[:32].encode("utf-8")

    salt = bytes.fromhex(enc_text[:64])
    iv = bytes.fromhex(enc_text[64:96])
    ciphertext = base64.b64decode(enc_text[96:])

    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=16, salt=salt, iterations=1000
    ).derive(passphrase)

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = sym_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# Main client class
# ---------------------------------------------------------------------------

class DhPension720:
    """연금복권 720+ 클라이언트"""

    def __init__(self, client):
        self.client = client
        self._jsessionid: Optional[str] = None

    # ------------------------------------------------------------------
    # Session helpers - 로또와 동일한 세션 재사용, JSESSIONID만 확보
    # ------------------------------------------------------------------

    async def _ensure_session(self):
        """el.dhlottery.co.kr DHJSESSIONID 확보

        로또와 동일한 aiohttp 세션을 그대로 사용.
        game.jsp 방문으로 DHJSESSIONID 쿠키를 받아온다.
        이미 확보된 경우 재사용.
        """
        if self._jsessionid:
            return

        # 1) cookie_jar에 이미 있는지 먼저 확인 (실제 쿠키명은 DHJSESSIONID)
        try:
            cookies = self.client.session.cookie_jar.filter_cookies(URL(EL_BASE_URL))
            for name in ("DHJSESSIONID", "JSESSIONID"):
                morsel = cookies.get(name)
                if morsel and getattr(morsel, "value", None):
                    self._jsessionid = morsel.value
                    _LOGGER.info(f"[PENSION720] cookie_jar에서 {name} 확보: {self._jsessionid[:8]}...")
                    return
        except Exception:
            pass

        # 2) game.jsp 방문해서 DHJSESSIONID 받기
        try:
            async with self.client.session.get(
                f"{EL_BASE_URL}/game/pension720/game.jsp",
                allow_redirects=True,
            ) as resp:
                await resp.text()
                # 응답 쿠키에서 DHJSESSIONID 또는 JSESSIONID 확인
                for k, v in resp.cookies.items():
                    if k.upper() in ("DHJSESSIONID", "JSESSIONID"):
                        self._jsessionid = v.value
                        _LOGGER.info(f"[PENSION720] 응답 쿠키에서 {k} 확보")
                        break
                # 없으면 cookie_jar 재확인
                if not self._jsessionid:
                    cookies = self.client.session.cookie_jar.filter_cookies(URL(EL_BASE_URL))
                    for name in ("DHJSESSIONID", "JSESSIONID"):
                        morsel = cookies.get(name)
                        if morsel and getattr(morsel, "value", None):
                            self._jsessionid = morsel.value
                            break
        except Exception as e:
            _LOGGER.warning(f"[PENSION720] game.jsp 방문 실패: {e}")

        if not self._jsessionid:
            raise DhPension720Error("DHJSESSIONID를 가져올 수 없습니다. el.dhlottery.co.kr 접근 불가")

        _LOGGER.info(f"[PENSION720] DHJSESSIONID 확보: {self._jsessionid[:8]}...")

    def _reset_session(self):
        """세션 리셋 (재시도 시 사용)"""
        self._jsessionid = None

    def _enc(self, form_data: str) -> str:
        return _encrypt(form_data, self._jsessionid)

    def _dec(self, enc_text: str) -> str:
        return _decrypt(enc_text, self._jsessionid)

    @staticmethod
    def _parse(decrypted: str) -> dict:
        """URL-encoded 응답 → dict"""
        params = parse_qs(decrypted, keep_blank_values=True)
        return {k: v[0] if len(v) == 1 else v for k, v in params.items()}

    # ------------------------------------------------------------------
    # 구매 가능 시간 체크 (로또와 동일 방식)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_buy_time():
        """연금복권 구매 가능 시간 확인 (KST 기준)"""
        now = datetime.datetime.now()
        if now.hour < 6:
            raise DhPension720PurchaseError(
                "[ERROR] 구매 시간이 아닙니다. (06:00~24:00 구매 가능)"
            )
        if now.weekday() == 5 and now.hour >= 20:
            raise DhPension720PurchaseError(
                "[ERROR] 토요일 20:00 이후 구매 불가. (일요일 06:00부터 가능)"
            )

    # ------------------------------------------------------------------
    # Round info
    # ------------------------------------------------------------------

    async def async_get_round_info(self) -> dict:
        """현재 회차 정보 조회.

        1차: el.dhlottery.co.kr/roundRemainTime.do (DHJSESSIONID 필요)
        실패 시 2차: www.dhlottery.co.kr 구매이력(P720)에서 최근 회차 추출
        """
        # 1차 시도: el.dhlottery.co.kr
        try:
            await self._ensure_session()
            resp = await self.client.session.get(
                f"{EL_BASE_URL}/roundRemainTime.do"
            )
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("round"):
                return data
            _LOGGER.warning(f"[PENSION720] roundRemainTime.do 응답 이상: {str(data)[:100]}")
        except Exception as e:
            _LOGGER.warning(f"[PENSION720] el. 회차 조회 실패, www 이력으로 fallback: {e}")

        # 2차 fallback: www 구매이력에서 최근 회차 추출
        try:
            items = await self.client.async_get_buy_list("P720")
            if items:
                latest_round = items[0].get("ltEpsd", 0)
                _LOGGER.info(f"[PENSION720] 구매이력 기반 회차: {latest_round}")
                return {"round": latest_round, "remainTime": None}
        except Exception as e:
            _LOGGER.warning(f"[PENSION720] 구매이력 회차 조회도 실패: {e}")

        return {"round": 0, "remainTime": None}

    # ------------------------------------------------------------------
    # Purchase
    # ------------------------------------------------------------------

    async def async_buy_1(self) -> DhPension720BuyData:
        """1조 자동 1장 구매"""
        return await self._async_buy([PensionTicket(group=1)])

    async def async_buy_5(self) -> DhPension720BuyData:
        """1~5조 자동 5장 구매 (조별 다른 번호, 서버 선택)"""
        return await self._async_buy([PensionTicket(group=g) for g in range(1, 6)])

    async def async_buy_random_all_groups(self) -> DhPension720BuyData:
        """랜덤 번호 1개를 뽑아 1~5조 전부 동일 번호로 구매"""
        number = random.randint(0, 999999)
        _LOGGER.info(f"[PURCHASE] 동일번호 5조 구매 - 번호: {number:06d}")
        return await self._async_buy([PensionTicket(group=g, number=number) for g in range(1, 6)])

    async def async_buy_manual(self, tickets: List[PensionTicket]) -> DhPension720BuyData:
        """수동 구매: 조와 번호를 직접 지정

        Args:
            tickets: PensionTicket 리스트 (group=1~5, number=0~999999)
        """
        if not tickets:
            raise DhPension720PurchaseError("구매할 티켓이 없습니다")
        for t in tickets:
            if not (1 <= t.group <= 5):
                raise DhPension720PurchaseError(f"조 번호 범위 오류: {t.group} (1~5)")
            if t.number is not None and not (0 <= t.number <= 999999):
                raise DhPension720PurchaseError(f"번호 범위 오류: {t.number} (0~999999)")
        return await self._async_buy(tickets)

    async def _async_buy(self, tickets: List[PensionTicket]) -> DhPension720BuyData:
        """
        연금복권 720+ 구매 (자동/수동 통합)

        Flow:
          0. 구매 시간 확인
          1. roundRemainTime.do  → 현재 회차 / 잔여시간
          2. makeOrderNo.do      → 주문번호 생성 (encrypted)
          3. connPro.do          → 구매 실행   (encrypted)
          4. checkDeposit.do     → 잔액 확인   (encrypted)
        """
        # ── Step 0: 구매 시간 확인 ────────────────────
        self._check_buy_time()

        # ── 세션 확보 (DHJSESSIONID) ─────────────────
        try:
            await self._ensure_session()
        except DhPension720Error:
            self._reset_session()
            await self.client.async_login()
            await self._ensure_session()

        ticket_count = len(tickets)
        is_manual = any(not t.is_auto for t in tickets)
        buy_type = "S" if is_manual else "A"

        # ── Step 1: 회차 확인 ─────────────────────────
        round_info = await self.async_get_round_info()
        current_round = round_info.get("round", 0)
        if not current_round:
            raise DhPension720PurchaseError("회차 정보를 가져올 수 없습니다")
        remain = round_info.get("remainTime")
        if remain is None:
            raise DhPension720PurchaseError("판매 잔여시간 확인 불가 (el.dhlottery.co.kr 세션 필요)")
        if remain <= 0:
            raise DhPension720PurchaseError("판매 마감되었습니다")

        _LOGGER.info(
            f"[PURCHASE] round={current_round}, remainTime={remain}, "
            f"tickets={[t.buy_no() for t in tickets]}, manual={is_manual}"
        )

        # ── frmauto: makeOrderNo.do용 파라미터 ────────
        # 수동: 대표 번호/조 전달 (단일이면 그대로, 다중이면 첫 번째)
        first_manual = next((t for t in tickets if not t.is_auto), None)
        frmauto = urlencode([
            ("ROUND", current_round),
            ("SEL_NO", f"{first_manual.number:06d}" if first_manual else ""),
            ("BUY_CNT", ""),
            ("AUTO_SEL_SET", ""),
            ("SEL_CLASS", str(first_manual.group) if first_manual and len(tickets) == 1 else ""),
            ("BUY_TYPE", buy_type),
            ("ACCS_TYPE", "01"),
        ])

        # ── Step 2: makeOrderNo.do ────────────────────
        resp1 = await self.client.session.post(
            f"{EL_BASE_URL}/makeOrderNo.do",
            data={"q": self._enc(frmauto)},
        )
        r1 = await resp1.json(content_type=None)
        if "q" not in r1:
            raise DhPension720PurchaseError(f"makeOrderNo 응답 오류: {r1}")

        p1 = self._parse(self._dec(r1["q"]))
        order_no = p1.get("orderNo", "")
        if not order_no:
            raise DhPension720PurchaseError("주문번호 생성 실패")
        _LOGGER.info(f"[PURCHASE] orderNo={order_no}")

        # ── Step 3: connPro.do ────────────────────────
        buy_nos = [t.buy_no() for t in tickets]
        buy_set_types = [t.set_type() for t in tickets]

        # 수동일 때 단일 필드 (마지막 수동 티켓 기준)
        last_manual = next((t for t in reversed(tickets) if not t.is_auto), None)
        set_type_val = "SE" if last_manual else "SA"
        classnum_val = str(last_manual.group) if last_manual else ""
        selnum_val = f"{last_manual.number:06d}" if last_manual else ""
        num_digits = list(f"{last_manual.number:06d}") if last_manual else [""] * 6

        frm = urlencode([
            ("ROUND", current_round),
            ("FLAG", ""),
            ("BUY_KIND", "01"),
            ("BUY_NO", ",".join(buy_nos)),
            ("BUY_CNT", ticket_count),
            ("BUY_SET_TYPE", ",".join(buy_set_types)),
            ("BUY_TYPE", buy_type),
            ("ACCS_TYPE", "01"),
            ("orderNo", order_no),
            ("orderDate", p1.get("orderDate", "")),
            ("TRANSACTION_ID", ""),
            ("WIN_DATE", ""),
            ("USER_ID", self.client.username),
            ("PAY_TYPE", ""),
            ("resultErrorCode", ""),
            ("resultErrorMsg", ""),
            ("resultOrderNo", ""),
            ("WORKING_FLAG", "false"),
            ("NUM_CHANGE_TYPE", ""),
            ("auto_process", ""),
            ("set_type", set_type_val),
            ("classnum", classnum_val),
            ("selnum", selnum_val),
            ("buytype", buy_type),
            ("num1", num_digits[0]),
            ("num2", num_digits[1]),
            ("num3", num_digits[2]),
            ("num4", num_digits[3]),
            ("num5", num_digits[4]),
            ("num6", num_digits[5]),
            ("DSEC", "0"),
            ("CLOSE_DATE", ""),
            ("verifyYN", "N"),
            ("curdeposit", "0"),
            ("curpay", "0"),
        ])

        resp2 = await self.client.session.post(
            f"{EL_BASE_URL}/connPro.do",
            data={"q": self._enc(frm)},
        )
        r2 = await resp2.json(content_type=None)
        if "q" not in r2:
            raise DhPension720PurchaseError(f"connPro 응답 오류: {r2}")

        p2 = self._parse(self._dec(r2["q"]))
        result_code = p2.get("resultCode", "")
        sale_cnt = int(p2.get("saleCnt", "0"))
        sale_ticket = p2.get("saleTicket", "")
        fail_cnt = int(p2.get("failCnt", "0"))
        fail_ticket = p2.get("failTicket", "")

        _LOGGER.info(
            f"[PURCHASE] resultCode={result_code}, saleCnt={sale_cnt}, failCnt={fail_cnt}"
        )

        if result_code == "120":
            raise DhPension720PurchaseError(f"구매 전체 실패: {fail_ticket}")
        if result_code not in ("100", "110"):
            raise DhPension720PurchaseError(f"구매 실패 (code={result_code})")
        if result_code == "110":
            _LOGGER.warning(f"[PURCHASE] 일부 실패: {fail_cnt}건 - {fail_ticket}")

        # ── Step 4: checkDeposit.do ───────────────────
        try:
            resp3 = await self.client.session.post(
                f"{EL_BASE_URL}/checkDeposit.do",
                data={"q": self._enc(frmauto)},
            )
            r3 = await resp3.json()
            if "q" in r3:
                self._parse(self._dec(r3["q"]))
        except Exception as e:
            _LOGGER.warning(f"[PURCHASE] checkDeposit 오류 (무시): {e}")

        return DhPension720BuyData(
            round_no=current_round,
            ticket_count=sale_cnt,
            tickets=sale_ticket,
            fail_count=fail_cnt,
            fail_tickets=fail_ticket,
            amount=sale_cnt * 1000,
        )

    # ------------------------------------------------------------------
    # History (www.dhlottery.co.kr 경유 - 로또와 동일한 API)
    # ------------------------------------------------------------------

    async def async_get_buy_history(self) -> List[DhPension720BuyHistoryData]:
        """구매 이력 조회 (www.dhlottery.co.kr - 로또와 동일 엔드포인트)"""
        try:
            items = await self.client.async_get_buy_list("P720")
            return [
                DhPension720BuyHistoryData(
                    round_no=item.get("ltEpsd", 0),
                    issue_dt=item.get("issueDay", ""),
                    barcode=item.get("gmInfo", ""),
                    ticket_count=item.get("prchsQty", 0),
                    amount=item.get("ntslAmt", 0),
                    result=item.get("ltWnResult", "미추첨"),
                )
                for item in items
            ]
        except Exception as e:
            _LOGGER.error(f"구매 이력 조회 오류: {e}")
            raise DhPension720Error(f"구매 이력 조회 실패: {e}")
