# -*- coding: utf-8 -*-
"""
동행복권 통합 Add-on v1.0.0
Home Assistant Add-on for DH Lottery (로또 6/45 + 연금복권 720+)
"""

import os
import asyncio
import logging
import time
from typing import Optional, Dict, List
from datetime import date, datetime, timezone, timedelta
from contextlib import asynccontextmanager

try:
    from zoneinfo import ZoneInfo
    _TZ_KST = ZoneInfo("Asia/Seoul")
except ImportError:
    _TZ_KST = timezone(timedelta(hours=9))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from dh_lottery_client import DhLotteryClient, DhLotteryError, DhLotteryLoginError
from dh_lotto_645 import DhLotto645, DhLotto645SelMode, DhLotto645Error
from dh_lotto_analyzer import DhLottoAnalyzer
from dh_pension_720 import DhPension720, DhPension720BuyData, DhPension720Error, DhPension720PurchaseError, PensionTicket
from mqtt_discovery import MQTTDiscovery, publish_sensor_mqtt, publish_button_mqtt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class AccountData:
    def __init__(self, username: str, password: str, enabled: bool = True):
        self.username = username
        self.password = password
        self.enabled = enabled
        self.client: Optional[DhLotteryClient] = None
        self.lotto_645: Optional[DhLotto645] = None
        self.analyzer: Optional[DhLottoAnalyzer] = None
        self.pension_720: Optional[DhPension720] = None
        self.manual_numbers_state = "auto,auto,auto,auto,auto,auto"
        self.pension_manual_state = "1,000000"   # 조,번호 형식 (예: "1,123456" / "all,123456")
        self.update_task: Optional[asyncio.Task] = None


config = {
    "accounts": [],
    "enable_lotto645": os.getenv("ENABLE_LOTTO645", "true").lower() == "true",
    "enable_pension720": os.getenv("ENABLE_PENSION720", "true").lower() == "true",
    "update_interval": int(os.getenv("UPDATE_INTERVAL", "3600")),
    "use_mqtt": os.getenv("USE_MQTT", "false").lower() == "true",
    "ha_url": os.getenv("HA_URL", "http://supervisor/core"),
    "supervisor_token": os.getenv("SUPERVISOR_TOKEN", ""),
}

accounts: Dict[str, AccountData] = {}
_last_purchase_time: Dict[tuple, float] = {}
mqtt_client: Optional[MQTTDiscovery] = None
event_loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _parse_yyyymmdd(text: str) -> Optional[str]:
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if len(text) != 8:
        return None
    try:
        d = date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
        return d.isoformat()
    except ValueError:
        return None


def is_purchase_available_now() -> bool:
    """동행복권 구매 가능 시간(KST): 평일/일 06:00-24:00, 토 06:00-20:00"""
    now = datetime.now(_TZ_KST)
    minutes = now.hour * 60 + now.minute
    if minutes < 360:
        return False
    if now.weekday() == 5 and minutes >= 1200:
        return False
    return True


def is_ingress_request(request: Request) -> bool:
    return (
        request.headers.get("X-Remote-User-Id") is not None
        or request.headers.get("X-Remote-User-Name") is not None
    )


# ---------------------------------------------------------------------------
# Sensor publishing
# ---------------------------------------------------------------------------

async def publish_sensor_for_account(account: AccountData, entity_id: str, state, attributes: dict = None):
    username = account.username

    if config["use_mqtt"] and mqtt_client and mqtt_client.connected:
        try:
            success = await publish_sensor_mqtt(
                mqtt_client=mqtt_client,
                entity_id=entity_id,
                state=state,
                username=username,
                attributes=attributes
            )
            if success:
                return
        except Exception as e:
            logger.error(f"[SENSOR][{username}] MQTT error: {e}")

    # REST API fallback
    import aiohttp
    if not config["supervisor_token"]:
        return

    url = f"{config['ha_url']}/api/states/sensor.addon_{username}_{entity_id}"
    headers = {
        "Authorization": f"Bearer {config['supervisor_token']}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"state": state, "attributes": attributes or {}},
                                    headers=headers, ssl=False) as resp:
                if resp.status not in [200, 201]:
                    logger.error(f"[SENSOR][{username}] REST failed: {resp.status}")
    except Exception as e:
        logger.error(f"[SENSOR][{username}] REST error: {e}")


# ---------------------------------------------------------------------------
# MQTT Button registration
# ---------------------------------------------------------------------------

async def register_buttons_for_account(account: AccountData):
    if not mqtt_client or not mqtt_client.connected:
        return

    username = account.username
    device_name = f"DH Lottery Addon ({username})"
    device_id = f"dhlotto_addon_{username}"

    # 로또 6/45 버튼
    if config["enable_lotto645"] and account.lotto_645:
        for button_id, button_name, icon in [
            ("lotto_buy_auto_1", "로또 1게임 자동구매", "mdi:ticket-confirmation"),
            ("lotto_buy_auto_5", "로또 5게임 자동구매", "mdi:ticket-confirmation-outline"),
            ("lotto_buy_manual", "로또 수동구매", "mdi:hand-pointing-right"),
            ("lotto_generate_random", "로또 랜덤번호 생성", "mdi:dice-multiple"),
        ]:
            topic = f"homeassistant/button/{mqtt_client.topic_prefix}_{username}_{button_id}/command"
            mqtt_client.publish_button_discovery(
                button_id=button_id, name=button_name, command_topic=topic,
                username=username, device_name=device_name,
                device_identifier=device_id, icon=icon,
            )

        # 수동번호 입력 텍스트
        input_state = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_manual_numbers/state"
        input_cmd = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_manual_numbers/set"
        mqtt_client.publish_input_text_discovery(
            input_id="manual_numbers",
            name="수동 번호 입력 (쉼표구분, 자동=auto)",
            state_topic=input_state, command_topic=input_cmd,
            username=username, device_name=device_name,
            device_identifier=device_id, icon="mdi:numeric", mode="text",
        )
        mqtt_client.client.publish(input_state, "auto,auto,auto,auto,auto,auto", qos=1, retain=True)

    # 연금복권 720+ 버튼
    if config["enable_pension720"] and account.pension_720:
        for button_id, button_name, icon in [
            ("pension_buy_1",        "연금복권 1장 자동구매",       "mdi:receipt"),
            ("pension_buy_5",        "연금복권 5장 자동구매(전조)", "mdi:receipt-text"),
            ("pension_buy_same_all", "연금복권 동일번호 5조 구매",  "mdi:numeric"),
            ("pension_buy_manual",   "연금복권 수동구매",           "mdi:hand-pointing-right"),
        ]:
            topic = f"homeassistant/button/{mqtt_client.topic_prefix}_{username}_{button_id}/command"
            mqtt_client.publish_button_discovery(
                button_id=button_id, name=button_name, command_topic=topic,
                username=username, device_name=device_name,
                device_identifier=device_id, icon=icon,
            )

        # 연금복권 수동번호 입력 텍스트 (형식: "조,번호" 예: "1,123456" / "all,123456")
        pension_input_state = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_pension_manual/state"
        pension_input_cmd = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_pension_manual/set"
        mqtt_client.publish_input_text_discovery(
            input_id="pension_manual",
            name="연금복권 수동 번호 (조,번호 / all,번호)",
            state_topic=pension_input_state, command_topic=pension_input_cmd,
            username=username, device_name=device_name,
            device_identifier=device_id, icon="mdi:numeric", mode="text",
        )
        mqtt_client.client.publish(pension_input_state, account.pension_manual_state, qos=1, retain=True)

    logger.info(f"[BUTTON][{username}] All buttons registered")


# ---------------------------------------------------------------------------
# MQTT Button command handler
# ---------------------------------------------------------------------------

def on_button_command(client_mqtt, userdata, message):
    try:
        topic = message.topic
        payload = message.payload.decode()
        logger.info(f"[MQTT] Received: topic={topic}, payload={payload}")

        parts = topic.split("/")
        if len(parts) < 3:
            return

        entity_id_full = parts[2]
        if not entity_id_full.startswith(mqtt_client.topic_prefix + "_"):
            return

        without_prefix = entity_id_full[len(mqtt_client.topic_prefix) + 1:]

        username = None
        for acc_username in accounts.keys():
            if without_prefix.startswith(acc_username + "_"):
                username = acc_username
                break

        if not username or username not in accounts:
            return

        account = accounts[username]

        # Input text 처리
        if "/text/" in topic and "/set" in topic:
            if "pension_manual" in topic:
                account.pension_manual_state = payload
                state_topic = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_pension_manual/state"
                client_mqtt.publish(state_topic, payload, qos=1, retain=True)
                logger.info(f"[INPUT][{username}] Pension manual updated: {payload}")
            else:
                account.manual_numbers_state = payload
                state_topic = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_manual_numbers/state"
                client_mqtt.publish(state_topic, payload, qos=1, retain=True)
                logger.info(f"[INPUT][{username}] Manual numbers updated: {payload}")
            return

        button_suffix = without_prefix[len(username) + 1:]
        logger.info(f"[BUTTON][{username}] Pressed: {button_suffix}")

        if not event_loop or not event_loop.is_running():
            return

        # 로또 버튼
        if button_suffix.startswith("lotto_"):
            asyncio.run_coroutine_threadsafe(
                execute_lotto_purchase(account, button_suffix),
                event_loop
            )
        # 연금복권 버튼
        elif button_suffix.startswith("pension_"):
            asyncio.run_coroutine_threadsafe(
                execute_pension_purchase(account, button_suffix),
                event_loop
            )

    except Exception as e:
        logger.error(f"[MQTT] Error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Purchase execution
# ---------------------------------------------------------------------------

async def execute_lotto_purchase(account: AccountData, button_id: str):
    username = account.username

    key = (username, button_id)
    now = time.monotonic()
    if key in _last_purchase_time and (now - _last_purchase_time[key]) < 15:
        logger.warning(f"[LOTTO][{username}] Duplicate press ignored: {button_id}")
        return

    if not account.lotto_645:
        logger.error(f"[LOTTO][{username}] Lotto 645 not enabled")
        return

    try:
        if button_id == "lotto_generate_random":
            random_numbers = DhLottoAnalyzer.generate_random_numbers(6)
            random_str = ",".join(map(str, random_numbers))
            account.manual_numbers_state = random_str
            state_topic = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_manual_numbers/state"
            mqtt_client.client.publish(state_topic, random_str, qos=1, retain=True)
            logger.info(f"[LOTTO][{username}] Random: {random_str}")
            return

        if button_id == "lotto_buy_manual":
            text = account.manual_numbers_state
            parts = [p.strip() for p in text.split(",")]
            if len(parts) != 6:
                await publish_sensor_for_account(account, "lotto45_purchase_error",
                    f"6개 값 필요 (현재 {len(parts)}개)", {"friendly_name": "구매 오류", "icon": "mdi:alert-circle"})
                return

            manual_numbers = []
            auto_count = 0
            for part in parts:
                if part.lower() == "auto":
                    auto_count += 1
                else:
                    try:
                        n = int(part)
                        if not (1 <= n <= 45):
                            raise ValueError(f"범위 오류: {n}")
                        manual_numbers.append(n)
                    except ValueError as e:
                        await publish_sensor_for_account(account, "lotto45_purchase_error",
                            str(e), {"friendly_name": "구매 오류", "icon": "mdi:alert-circle"})
                        return

            if len(manual_numbers) != len(set(manual_numbers)):
                await publish_sensor_for_account(account, "lotto45_purchase_error",
                    "중복 번호", {"friendly_name": "구매 오류", "icon": "mdi:alert-circle"})
                return

            if auto_count == 0:
                mode = DhLotto645SelMode.MANUAL
            elif auto_count == 6:
                mode = DhLotto645SelMode.AUTO
            else:
                mode = DhLotto645SelMode.SEMI_AUTO

            slots = [DhLotto645.Slot(mode=mode, numbers=sorted(manual_numbers))]
            result = await account.lotto_645.async_buy(slots, max_games=1)
        else:
            count = 5 if button_id == "lotto_buy_auto_5" else 1
            slots = [DhLotto645.Slot(mode=DhLotto645SelMode.AUTO, numbers=[]) for _ in range(count)]
            result = await account.lotto_645.async_buy(slots)

        logger.info(f"[LOTTO][{username}] Success! Round: {result.round_no}")
        _last_purchase_time[key] = time.monotonic()
        await update_sensors_for_account(account)

    except DhLotto645Error as e:
        logger.warning(f"[LOTTO][{username}] Purchase rejected: {e}")
        await publish_sensor_for_account(account, "lotto45_purchase_error", str(e)[:255], {
            "error": str(e), "friendly_name": "구매 오류", "icon": "mdi:alert-circle",
        })
    except Exception as e:
        logger.error(f"[LOTTO][{username}] Failed: {e}", exc_info=True)
        await publish_sensor_for_account(account, "lotto45_purchase_error", str(e)[:255], {
            "error": str(e), "friendly_name": "구매 오류", "icon": "mdi:alert-circle",
        })


def _parse_pension_manual(text: str) -> List[PensionTicket]:
    """연금복권 수동 입력 파싱.

    형식: "조,번호" (예: "1,123456") 또는 "all,번호" (1~5조 전부 동일번호)
    번호는 0~999999 사이 정수 또는 6자리 문자열.
    """
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        raise DhPension720PurchaseError(
            f"입력 형식 오류: '{text}' (올바른 형식: '조,번호' 예: '1,123456' 또는 'all,123456')"
        )

    group_str, number_str = parts[0].lower(), parts[1]

    try:
        number = int(number_str)
    except ValueError:
        raise DhPension720PurchaseError(f"번호 형식 오류: '{number_str}' (0~999999 정수)")
    if not (0 <= number <= 999999):
        raise DhPension720PurchaseError(f"번호 범위 오류: {number} (0~999999)")

    if group_str == "all":
        return [PensionTicket(group=g, number=number) for g in range(1, 6)]

    try:
        group = int(group_str)
    except ValueError:
        raise DhPension720PurchaseError(f"조 형식 오류: '{group_str}' (1~5 또는 'all')")
    if not (1 <= group <= 5):
        raise DhPension720PurchaseError(f"조 범위 오류: {group} (1~5)")

    return [PensionTicket(group=group, number=number)]


async def execute_pension_purchase(account: AccountData, button_id: str):
    username = account.username

    key = (username, button_id)
    now = time.monotonic()
    if key in _last_purchase_time and (now - _last_purchase_time[key]) < 15:
        logger.warning(f"[PENSION][{username}] Duplicate press ignored: {button_id}")
        return

    if not account.pension_720:
        logger.error(f"[PENSION][{username}] Pension 720+ not enabled")
        return

    try:
        if button_id == "pension_buy_5":
            buy_data = await account.pension_720.async_buy_5()
        elif button_id == "pension_buy_same_all":
            buy_data = await account.pension_720.async_buy_random_all_groups()
        elif button_id == "pension_buy_manual":
            tickets = _parse_pension_manual(account.pension_manual_state)
            buy_data = await account.pension_720.async_buy_manual(tickets)
        else:
            buy_data = await account.pension_720.async_buy_1()

        logger.info(f"[PENSION][{username}] Success! Round: {buy_data.round_no}, tickets: {buy_data.ticket_count}")
        _last_purchase_time[key] = time.monotonic()
        await update_sensors_for_account(account)

    except DhPension720PurchaseError as e:
        logger.warning(f"[PENSION][{username}] Purchase rejected: {e}")
        await publish_sensor_for_account(account, "pension720_error", str(e)[:255], {
            "error": str(e), "friendly_name": "연금복권 오류", "icon": "mdi:alert-circle",
        })
    except Exception as e:
        logger.error(f"[PENSION][{username}] Failed: {e}", exc_info=True)
        await publish_sensor_for_account(account, "pension720_error", str(e)[:255], {
            "error": str(e), "friendly_name": "연금복권 오류", "icon": "mdi:alert-circle",
        })


# ---------------------------------------------------------------------------
# Sensor updates
# ---------------------------------------------------------------------------

async def update_sensors_for_account(account: AccountData):
    username = account.username

    if not is_purchase_available_now():
        logger.info(f"[SENSOR][{username}] 구매 불가 시간 - 업데이트 스킵")
        return

    if not account.client or not account.client.logged_in:
        logger.warning(f"[SENSOR][{username}] 미로그인 - 로그인 시도...")
        try:
            await account.client.async_login()
        except Exception as e:
            logger.error(f"[SENSOR][{username}] 로그인 실패: {e}")
            await publish_sensor_for_account(account, "lotto45_login_error", str(e)[:255], {
                "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat(),
                "friendly_name": "로그인 오류", "icon": "mdi:account-alert",
            })
            return

    try:
        logger.info(f"[SENSOR][{username}] 센서 업데이트 중...")

        # 공통: 잔액
        balance = await account.client.async_get_balance()
        await publish_sensor_for_account(account, "lotto45_balance", balance.deposit, {
            "purchase_available": balance.purchase_available,
            "reservation_purchase": balance.reservation_purchase,
            "withdrawal_request": balance.withdrawal_request,
            "this_month_accumulated": balance.this_month_accumulated_purchase,
            "unit_of_measurement": "KRW",
            "friendly_name": "예치금",
            "icon": "mdi:wallet",
        })

        # 구매 가능 시간 정보
        await publish_sensor_for_account(account, "lotto45_purchase_available_time",
            "평일/일: 06:00-24:00, 토: 06:00-20:00", {
            "weekdays": "06:00-24:00", "saturday": "06:00-20:00",
            "friendly_name": "구매 가능 시간", "icon": "mdi:clock-time-eight",
        })

        # 로또 6/45 센서
        if config["enable_lotto645"] and account.lotto_645:
            await _update_lotto645_sensors(account)

        # 연금복권 720+ 센서
        if config["enable_pension720"] and account.pension_720:
            await _update_pension720_sensors(account)

        now = datetime.now(timezone.utc).isoformat()
        await publish_sensor_for_account(account, "dhlottery_last_update", now, {
            "friendly_name": "마지막 업데이트", "icon": "mdi:clock-check-outline",
        })
        logger.info(f"[SENSOR][{username}] 업데이트 완료")

    except DhLotteryLoginError as e:
        if account.client:
            account.client.logged_in = False
        await publish_sensor_for_account(account, "lotto45_login_error", str(e)[:255], {
            "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat(),
            "friendly_name": "로그인 오류", "icon": "mdi:account-alert",
        })
        logger.warning(f"[SENSOR][{username}] 로그인 오류: {e}")
    except DhLotteryError as e:
        if account.client:
            account.client.logged_in = False
        await publish_sensor_for_account(account, "lotto45_login_error", str(e)[:255], {
            "error": str(e), "timestamp": datetime.now(timezone.utc).isoformat(),
            "friendly_name": "API 오류", "icon": "mdi:account-alert",
        })
        logger.warning(f"[SENSOR][{username}] API 오류: {e}")
    except Exception as e:
        logger.error(f"[SENSOR][{username}] 업데이트 실패: {e}", exc_info=True)


async def _update_lotto645_sensors(account: AccountData):
    username = account.username
    try:
        params = {"_": int(datetime.now().timestamp() * 1000)}
        raw_data = await account.client.async_get('lt645/selectPstLt645Info.do', params)

        items = raw_data.get('list', [])
        if items:
            item = items[0]
            latest_round_info = await account.lotto_645.async_get_round_info()
            result_item = {
                "ltEpsd": latest_round_info.round_no,
                "tm1WnNo": latest_round_info.numbers[0],
                "tm2WnNo": latest_round_info.numbers[1],
                "tm3WnNo": latest_round_info.numbers[2],
                "tm4WnNo": latest_round_info.numbers[3],
                "tm5WnNo": latest_round_info.numbers[4],
                "tm6WnNo": latest_round_info.numbers[5],
                "bnsWnNo": latest_round_info.bonus_num,
                "ltRflYmd": latest_round_info.draw_date,
            }

            round_no = _safe_int(result_item.get("ltEpsd"))
            winning_numbers = [_safe_int(result_item.get(f"tm{i}WnNo")) for i in range(1, 7)]
            bonus_number = _safe_int(result_item.get("bnsWnNo"))

            await publish_sensor_for_account(account, "lotto645_round", round_no, {
                "friendly_name": "로또 회차", "icon": "mdi:counter",
            })
            await publish_sensor_for_account(account, "lotto645_winning_numbers",
                f"Round {round_no}: {', '.join(map(str, winning_numbers))} + {bonus_number}", {
                "numbers": winning_numbers, "bonus": bonus_number, "round": round_no,
                "friendly_name": "당첨 번호", "icon": "mdi:trophy-award",
            })
            for i in range(1, 7):
                await publish_sensor_for_account(account, f"lotto645_number{i}",
                    winning_numbers[i - 1], {"friendly_name": f"로또 번호 {i}", "icon": f"mdi:numeric-{i}-circle"})
            await publish_sensor_for_account(account, "lotto645_bonus", bonus_number, {
                "friendly_name": "로또 보너스", "icon": "mdi:star-circle",
            })

            draw_date = _parse_yyyymmdd(result_item.get("ltRflYmd"))
            if draw_date:
                await publish_sensor_for_account(account, "lotto645_draw_date", draw_date, {
                    "friendly_name": "추첨일", "icon": "mdi:calendar", "device_class": "date",
                })

            for rank in range(1, 6):
                rank_names = ["first", "second", "third", "fourth", "fifth"]
                rank_icons = ["mdi:trophy", "mdi:medal", "mdi:medal-outline", "mdi:currency-krw", "mdi:cash"]
                await publish_sensor_for_account(account, f"lotto645_{rank_names[rank-1]}_prize",
                    _safe_int(item.get(f"rnk{rank}WnAmt")), {
                    "friendly_name": f"로또 {rank}등 당첨금", "unit_of_measurement": "KRW",
                    "total_amount": _safe_int(item.get(f"rnk{rank}SumWnAmt")),
                    "winners": _safe_int(item.get(f"rnk{rank}WnNope")),
                    "icon": rank_icons[rank - 1],
                })

    except Exception as e:
        logger.warning(f"[LOTTO][{username}] 당첨번호 조회 실패: {e}")

    # 구매 이력 및 게임 결과
    try:
        history = await account.lotto_645.async_get_buy_history_this_week()
        all_games = []
        if history:
            latest_purchase = history[0]
            await publish_sensor_for_account(account, "lotto45_latest_purchase", latest_purchase.round_no, {
                "round_no": latest_purchase.round_no, "barcode": latest_purchase.barcode,
                "result": latest_purchase.result,
                "games": [{"slot": g.slot, "mode": str(g.mode), "numbers": g.numbers}
                          for g in latest_purchase.games],
                "games_count": len(latest_purchase.games),
                "friendly_name": "로또 최근 구매", "icon": "mdi:receipt-text",
            })
            for purchase in reversed(history):
                for game in purchase.games:
                    all_games.append({"game": game, "round_no": purchase.round_no, "result": purchase.result})
                    if len(all_games) >= 5:
                        break
                if len(all_games) >= 5:
                    break

        latest_round_no = await account.lotto_645.async_get_latest_round_no()
        weekly_purchase_count = 0

        for i in range(1, 6):
            if i <= len(all_games):
                gi = all_games[i - 1]
                game = gi["game"]
                round_no = gi["round_no"]
                numbers_str = ", ".join(map(str, game.numbers))
                await publish_sensor_for_account(account, f"lotto45_game_{i}", numbers_str, {
                    "slot": game.slot, "슬롯": game.slot,
                    "mode": str(game.mode), "선택": str(game.mode),
                    "numbers": game.numbers, "round_no": round_no, "result": gi["result"],
                    "friendly_name": f"게임 {i}", "icon": f"mdi:numeric-{i}-box-multiple",
                })

                try:
                    result_text = "미추첨"
                    result_icon = "mdi:clock-outline"
                    matching_count = 0
                    bonus_match = False
                    rank = 0
                    winning_numbers_check = []
                    bonus_number_check = 0

                    if round_no <= latest_round_no:
                        winning_data = await account.lotto_645.async_get_round_info(round_no)
                        winning_numbers_check = winning_data.numbers
                        bonus_number_check = winning_data.bonus_num
                        check_result = await account.analyzer.async_check_winning(game.numbers, round_no)
                        matching_count = check_result["matching_count"]
                        bonus_match = check_result["bonus_match"]
                        rank = check_result["rank"]
                        rank_labels = ["낙첨", "1등 당첨", "2등 당첨", "3등 당첨", "4등 당첨", "5등 당첨"]
                        rank_icons = ["mdi:close-circle-outline", "mdi:trophy", "mdi:medal",
                                      "mdi:medal-outline", "mdi:currency-krw", "mdi:cash"]
                        result_text = rank_labels[rank] if 0 <= rank <= 5 else "낙첨"
                        result_icon = rank_icons[rank] if 0 <= rank <= 5 else "mdi:close-circle-outline"
                    else:
                        weekly_purchase_count += 1

                    await publish_sensor_for_account(account, f"lotto45_game_{i}_result", result_text, {
                        "round_no": round_no, "my_numbers": game.numbers,
                        "winning_numbers": winning_numbers_check,
                        "bonus_number": bonus_number_check,
                        "matching_count": matching_count, "bonus_match": bonus_match, "rank": rank,
                        "friendly_name": f"게임 {i} 결과", "icon": result_icon,
                    })
                except Exception as e:
                    logger.warning(f"[LOTTO][{username}] 게임 {i} 결과 확인 실패: {e}")
                    await publish_sensor_for_account(account, f"lotto45_game_{i}_result", "확인 실패", {
                        "round_no": round_no, "my_numbers": game.numbers,
                        "friendly_name": f"게임 {i} 결과", "icon": "mdi:alert-circle-outline",
                    })
            else:
                await publish_sensor_for_account(account, f"lotto45_game_{i}", "Empty", {
                    "slot": "-", "슬롯": "-", "mode": "-", "선택": "-",
                    "numbers": [], "round_no": 0, "result": "-",
                    "friendly_name": f"게임 {i}", "icon": f"mdi:numeric-{i}-box-outline",
                })
                await publish_sensor_for_account(account, f"lotto45_game_{i}_result", "Empty", {
                    "round_no": 0, "my_numbers": [], "winning_numbers": [], "bonus_number": 0,
                    "matching_count": 0, "bonus_match": False, "rank": 0,
                    "friendly_name": f"게임 {i} 결과", "icon": "mdi:circle-outline",
                })

        weekly_limit = 5
        await publish_sensor_for_account(account, "lotto45_weekly_purchase_count", weekly_purchase_count, {
            "weekly_limit": weekly_limit,
            "remaining": max(0, weekly_limit - weekly_purchase_count),
            "friendly_name": "주간 구매 횟수", "unit_of_measurement": "games",
            "icon": "mdi:ticket-confirmation" if weekly_purchase_count < weekly_limit else "mdi:close-circle",
        })

    except Exception as e:
        logger.warning(f"[LOTTO][{username}] 구매이력 조회 실패: {e}")

    # 핫/콜드 번호
    try:
        if account.analyzer:
            hc = await account.analyzer.async_get_hot_cold_numbers(recent_rounds=20)
            await publish_sensor_for_account(account, "lotto45_hot_numbers",
                ", ".join(map(str, hc.hot_numbers)), {
                "numbers": hc.hot_numbers,
                "friendly_name": "핫 번호 10개 (최근 20회)", "icon": "mdi:fire",
            })
            await publish_sensor_for_account(account, "lotto45_cold_numbers",
                ", ".join(map(str, hc.cold_numbers)), {
                "numbers": hc.cold_numbers,
                "friendly_name": "콜드 번호 10개 (최근 20회)", "icon": "mdi:snowflake",
            })
    except Exception as e:
        logger.warning(f"[LOTTO][{username}] 핫/콜드 번호 조회 실패: {e}")


async def _update_pension720_sensors(account: AccountData):
    username = account.username

    # 잔액은 로또와 동일한 www 공통 API 사용 (이미 update_sensors_for_account에서 조회됨)
    try:
        balance = await account.client.async_get_balance()
        await publish_sensor_for_account(account, "pension720_balance", balance.purchase_available, {
            "friendly_name": "연금복권 구매가능금액", "icon": "mdi:wallet",
            "unit_of_measurement": "KRW", "deposit": balance.deposit,
            "purchase_available": balance.purchase_available,
        })
        await publish_sensor_for_account(account, "pension720_error", "", {
            "friendly_name": "연금복권 오류", "icon": "mdi:account-check",
        })
    except Exception as e:
        logger.warning(f"[PENSION][{username}] 잔액 조회 실패: {e}")
        await publish_sensor_for_account(account, "pension720_error", str(e)[:255], {
            "error": str(e), "friendly_name": "연금복권 오류", "icon": "mdi:account-alert",
        })

    # 구매 이력
    try:
        history = await account.pension_720.async_get_buy_history()
        if history:
            latest = history[0]
            await publish_sensor_for_account(account, "pension720_latest_purchase", latest.round_no, {
                "round_no": latest.round_no,
                "barcode": latest.barcode,
                "ticket_count": latest.ticket_count,
                "amount": latest.amount,
                "result": latest.result,
                "friendly_name": "연금복권 최근 구매", "icon": "mdi:receipt-text",
            })
        round_info = await account.pension_720.async_get_round_info()
        current_round = round_info.get("round", 0)
        await publish_sensor_for_account(account, "pension720_round", current_round, {
            "friendly_name": "연금복권 회차", "icon": "mdi:counter",
        })
    except Exception as e:
        logger.warning(f"[PENSION][{username}] 이력/회차 조회 실패: {e}")


# ---------------------------------------------------------------------------
# Account initialization
# ---------------------------------------------------------------------------

def load_accounts_from_env():
    import json
    try:
        accounts_list = json.loads(os.getenv("ACCOUNTS", "[]"))
        config["accounts"] = accounts_list
        logger.info(f"계정 {len(accounts_list)}개 로드됨")
        for i, acc in enumerate(accounts_list, 1):
            logger.info(f"  계정 {i}: {acc.get('username', '')} (enabled: {acc.get('enabled', True)})")
    except Exception as e:
        logger.error(f"계정 파싱 실패: {e}")
        config["accounts"] = []


async def init_account(account: AccountData) -> bool:
    username = account.username
    if not account.enabled:
        logger.info(f"[INIT][{username}] 비활성화 상태")
        return False

    try:
        logger.info(f"[INIT][{username}] 초기화 중...")
        account.client = DhLotteryClient(account.username, account.password)
        await account.client.async_login()

        if config["enable_lotto645"]:
            account.lotto_645 = DhLotto645(account.client)
            account.analyzer = DhLottoAnalyzer(account.client)

        if config["enable_pension720"]:
            account.pension_720 = DhPension720(account.client)

        logger.info(f"[INIT][{username}] 성공")
        return True
    except Exception as e:
        logger.error(f"[INIT][{username}] 실패: {e}", exc_info=True)
        return False


async def init_clients():
    global mqtt_client

    load_accounts_from_env()

    if not config["accounts"]:
        logger.error("설정된 계정 없음")
        return False

    success_count = 0
    for acc_config in config["accounts"]:
        username = acc_config.get("username", "")
        password = acc_config.get("password", "")
        enabled = acc_config.get("enabled", True)

        if not username or not password:
            continue

        account = AccountData(username, password, enabled)
        accounts[username] = account

        if await init_account(account):
            success_count += 1

    logger.info(f"계정 초기화: {success_count}/{len(accounts)}")

    # MQTT 설정
    if config["use_mqtt"]:
        logger.info("MQTT 초기화 중...")
        mqtt_client = MQTTDiscovery(
            mqtt_url=os.getenv("MQTT_URL", "mqtt://homeassistant.local:1883"),
            username=os.getenv("MQTT_USERNAME"),
            password=os.getenv("MQTT_PASSWORD"),
        )

        if mqtt_client.connect():
            logger.info("MQTT 연결됨")

            for account in accounts.values():
                if account.enabled and account.client and account.client.logged_in:
                    await register_buttons_for_account(account)

            # 버튼 명령 구독
            mqtt_client.client.on_message = on_button_command
            for account in accounts.values():
                if not (account.enabled and account.client and account.client.logged_in):
                    continue
                username = account.username

                lotto_buttons = ["lotto_buy_auto_1", "lotto_buy_auto_5", "lotto_buy_manual", "lotto_generate_random"]
                pension_buttons = ["pension_buy_1", "pension_buy_5", "pension_buy_same_all", "pension_buy_manual"]

                for btn in lotto_buttons:
                    topic = f"homeassistant/button/{mqtt_client.topic_prefix}_{username}_{btn}/command"
                    mqtt_client.client.subscribe(topic)

                for btn in pension_buttons:
                    topic = f"homeassistant/button/{mqtt_client.topic_prefix}_{username}_{btn}/command"
                    mqtt_client.client.subscribe(topic)

                input_topic = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_manual_numbers/set"
                mqtt_client.client.subscribe(input_topic)

                pension_input_topic = f"homeassistant/text/{mqtt_client.topic_prefix}_{username}_pension_manual/set"
                mqtt_client.client.subscribe(pension_input_topic)

            logger.info("MQTT 구독 완료")
        else:
            logger.warning("MQTT 연결 실패")
            mqtt_client = None

    return success_count > 0


async def cleanup_clients():
    global mqtt_client
    if mqtt_client:
        try:
            mqtt_client.disconnect()
        except Exception:
            pass
    for account in accounts.values():
        if account.client:
            try:
                await account.client.close()
            except Exception:
                pass


async def background_tasks_for_account(account: AccountData):
    username = account.username

    if not account.client or not account.client.logged_in:
        logger.warning(f"[BG][{username}] 미로그인 - 백그라운드 작업 스킵")
        return

    await asyncio.sleep(10)

    while True:
        try:
            if not is_purchase_available_now():
                logger.info(f"[BG][{username}] 구매 불가 시간 - 동기화 스킵")
                await asyncio.sleep(config["update_interval"])
                continue
            await update_sensors_for_account(account)
            await asyncio.sleep(config["update_interval"])
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[BG][{username}] 오류: {e}", exc_info=True)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global event_loop
    logger.info("동행복권 Add-on v1.0.0 시작 중...")
    event_loop = asyncio.get_running_loop()
    await init_clients()

    tasks = []
    for account in accounts.values():
        if account.enabled:
            task = asyncio.create_task(background_tasks_for_account(account))
            account.update_task = task
            tasks.append(task)

    logger.info("시작 완료")
    yield

    logger.info("종료 중...")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await cleanup_clients()


app = FastAPI(title="동행복권 Add-on", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Ingress-Request"],
)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    accounts_html = "<ul>"
    for username, account in accounts.items():
        status = "✅" if account.client and account.client.logged_in else "❌"
        lotto = "🎰" if account.lotto_645 else ""
        pension = "🎫" if account.pension_720 else ""
        accounts_html += f"<li><strong>{username}</strong>: {status} {lotto}{pension}</li>"
    accounts_html += "</ul>"
    ingress_badge = (
        '<span style="background:#0d47a1;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;">Ingress</span>'
        if is_ingress_request(request) else ""
    )
    lotto_enabled = "✅" if config["enable_lotto645"] else "❌"
    pension_enabled = "✅" if config["enable_pension720"] else "❌"
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>동행복권 v1.0.0</title>
    <style>body {{ font-family: Arial; margin: 40px; }} .info {{ background: #f5f5f5; padding: 15px; border-radius: 5px; margin: 20px 0; }}</style>
</head>
<body>
    <h1>🎰 동행복권 <span style="color:#666;">v1.0.0</span> {ingress_badge}</h1>
    <div class="info">
        <p>로또 6/45: {lotto_enabled} &nbsp; 연금복권 720+: {pension_enabled}</p>
        <h2>계정 ({len(accounts)})</h2>
        {accounts_html}
    </div>
    <ul>
        <li><a href="health">Health Check</a></li>
        <li><a href="accounts">계정 목록</a></li>
    </ul>
</body>
</html>"""


@app.get("/health")
async def health(request: Request):
    accounts_status = {}
    logged_in_count = 0
    for username, account in accounts.items():
        is_logged_in = bool(account.client and getattr(account.client, "logged_in", False))
        if is_logged_in:
            logged_in_count += 1
        accounts_status[username] = {
            "logged_in": is_logged_in,
            "enabled": account.enabled,
            "lotto645": account.lotto_645 is not None,
            "pension720": account.pension_720 is not None,
            "status": "✅ Active" if is_logged_in else "❌ Login Failed",
        }

    status = "ok" if (len(accounts) == 0 or logged_in_count > 0) else "degraded"
    return {
        "status": status,
        "version": "1.0.0",
        "ingress": is_ingress_request(request),
        "enable_lotto645": config["enable_lotto645"],
        "enable_pension720": config["enable_pension720"],
        "accounts": accounts_status,
        "total_accounts": len(accounts),
        "logged_in_accounts": logged_in_count,
    }


@app.get("/accounts")
async def list_accounts():
    return {"accounts": [
        {
            "username": u,
            "enabled": a.enabled,
            "logged_in": a.client.logged_in if a.client else False,
            "lotto645": a.lotto_645 is not None,
            "pension720": a.pension_720 is not None,
        }
        for u, a in accounts.items()
    ]}


# --- 로또 6/45 REST API ---

@app.post("/api/lotto/{username}/buy_auto/{count}")
async def lotto_buy_auto(username: str, count: int):
    account = accounts.get(username)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.lotto_645:
        raise HTTPException(status_code=400, detail="Lotto 645 not enabled")
    if count not in (1, 5):
        raise HTTPException(status_code=400, detail="count must be 1 or 5")

    key = (username, f"lotto_buy_auto_{count}")
    now = time.time()
    if key in _last_purchase_time and now - _last_purchase_time[key] < 15:
        raise HTTPException(status_code=429, detail="Too soon, wait 15s")

    try:
        _last_purchase_time[key] = now
        slots = [DhLotto645.Slot(mode=DhLotto645SelMode.AUTO, numbers=[]) for _ in range(count)]
        result = await account.lotto_645.async_buy(slots)
        await update_sensors_for_account(account)
        return {"success": True, "round_no": result.round_no, "games": len(result.games)}
    except DhLotto645Error as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[API][{username}] 로또 구매 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Purchase failed")


# --- 연금복권 720+ REST API ---

@app.post("/api/pension/{username}/buy/{count}")
async def pension_buy(username: str, count: int):
    account = accounts.get(username)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.pension_720:
        raise HTTPException(status_code=400, detail="Pension 720+ not enabled")
    if count not in (1, 5):
        raise HTTPException(status_code=400, detail="count must be 1 or 5")

    key = (username, f"pension_buy_{count}")
    now = time.time()
    if key in _last_purchase_time and now - _last_purchase_time[key] < 15:
        raise HTTPException(status_code=429, detail="Too soon, wait 15s")

    try:
        _last_purchase_time[key] = now
        buy_data = await account.pension_720.async_buy_5() if count == 5 else await account.pension_720.async_buy_1()
        await update_sensors_for_account(account)
        return {
            "success": True,
            "round_no": buy_data.round_no,
            "ticket_count": buy_data.ticket_count,
            "tickets": buy_data.tickets,
            "fail_count": buy_data.fail_count,
            "amount": buy_data.amount,
        }
    except DhPension720PurchaseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[API][{username}] 연금복권 구매 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Purchase failed")


@app.get("/api/balance/{username}")
async def get_balance(username: str):
    account = accounts.get(username)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        balance = await account.client.async_get_balance()
        return {
            "deposit": balance.deposit,
            "purchase_available": balance.purchase_available,
            "reservation_purchase": balance.reservation_purchase,
            "withdrawal_request": balance.withdrawal_request,
            "this_month_accumulated": balance.this_month_accumulated_purchase,
        }
    except Exception as e:
        logger.error(f"[API][{username}] 잔액 조회 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get balance")


@app.get("/api/history/{username}")
async def get_history(username: str):
    account = accounts.get(username)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    result = {}
    if account.lotto_645:
        try:
            history = await account.lotto_645.async_get_buy_history_this_week()
            result["lotto645"] = [
                {
                    "round_no": h.round_no,
                    "barcode": h.barcode,
                    "result": h.result,
                    "games": [{"slot": g.slot, "mode": str(g.mode), "numbers": g.numbers}
                               for g in h.games],
                }
                for h in history
            ]
        except Exception as e:
            result["lotto645_error"] = str(e)

    if account.pension_720:
        try:
            history = await account.pension_720.async_get_buy_history()
            result["pension720"] = [
                {
                    "round_no": h.round_no,
                    "issue_dt": h.issue_dt,
                    "barcode": h.barcode,
                    "ticket_count": h.ticket_count,
                    "amount": h.amount,
                    "result": h.result,
                }
                for h in history
            ]
        except Exception as e:
            result["pension720_error"] = str(e)

    return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=60100, log_level="info")
