"""查天气：走 Open-Meteo（免费、无需 Key，只用项目已有的 requests）。

两步：先用城市名换经纬度（geocoding），再按经纬度取当前天气与逐日预报。结果整理成
一段**给模型看**的紧凑文本，由模型自己组织成话说给用户——技能不负责措辞。

城市从哪来（按优先级）：调用参数 → 设置里的默认城市 → 都没有就提示模型去问用户。
用户说过的城市会作为事实进长期记忆，模型能从上下文里看到并填进参数里。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import requests

from app import devtools
from app.config import load_settings

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CONNECT_TIMEOUT = 6
READ_TIMEOUT = 10
#: 最多查几天（含今天）。当前天气 + 未来三天
MAX_DAYS = 4
#: 同一城市同一范围的结果缓存这么久，省得模型连着问两次就发两次请求
CACHE_SECONDS = 600
#: 城市名 → 经纬度的结果也缓存久一点：地名解析很少变
GEOCODE_CACHE_SECONDS = 24 * 3600

#: WMO 天气码 → 中文说法（Open-Meteo 用的就是这套码）
_WMO: dict[int, str] = {
    0: "晴",
    1: "晴间少云",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "较强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷阵雨伴冰雹",
}

_WEEKDAYS = "一二三四五六日"
#: 结果缓存：``键 -> (写入时刻, 文本)``
_CACHE: dict[str, tuple[float, str]] = {}
#: 地名解析缓存：``城市名 -> (写入时刻, 地点字典)``
_GEOCODE: dict[str, tuple[float, dict[str, Any] | None]] = {}
#: 与 LLM 请求同样的口径：忽略系统代理，避免被为别的用途配的代理拖住
_SESSION = requests.Session()
_SESSION.trust_env = False


def weather_text(code: Any) -> str:
    """WMO 天气码 → 中文；认不出的码就说"未知"。"""
    try:
        return _WMO.get(int(code), "未知天气")
    except (TypeError, ValueError):
        return "未知天气"


class WeatherSkill:
    """查当前天气与未来几天预报。"""

    name = "get_weather"
    label = "查天气"
    description = (
        "查询某个城市的当前天气与未来几天预报。用户问天气、要不要带伞、今天热不热这类"
        "话题时用它。city 填用户提到的城市；用户没说、但对话或长期记忆里知道他在哪个城市"
        "（例如记着「人在上海」）就填那个；确实不知道就留空，会用设置里的默认城市，仍然"
        "没有时会让你先去问用户。不要自己编一个城市。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "城市名，如“上海”“杭州”“Tokyo”。留空表示用设置里记录的默认城市",
            },
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DAYS,
                "description": (
                    f"要几天的预报（含今天），最多 {MAX_DAYS}。只问现在填 1；"
                    "问到明天 / 未来几天就填 2 或更多"
                ),
            },
        },
        "required": [],
    }

    # ---- 执行 ---------------------------------------------------------------

    def run(self, arguments: dict[str, Any]) -> str:
        city = str(arguments.get("city") or "").strip() or self._default_city()
        if not city:
            return (
                "还不知道用户在哪个城市：设置里没有默认城市，你也没有别的依据。"
                "请先问用户所在城市，拿到之后再调用一次本工具并把城市填进 city。"
            )
        days = self._days(arguments.get("days"))
        key = f"{city}|{days}"
        cached = _CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            place = self._geocode(city)
            if place is None:
                return f"没找到城市「{city}」：换个说法再试（中文名或英文名都行），或者问用户确认一下。"
            text = self._forecast(place, days)
        except requests.RequestException as exc:
            devtools.log.warning("技能 · 查天气失败：%s", exc)
            return f"查天气失败（网络不通）：{exc}。可以过一会儿再试，或直接告诉用户暂时查不到。"
        _CACHE[key] = (time.monotonic(), text)
        return text

    @staticmethod
    def _default_city() -> str:
        """设置里的默认城市（可留空）。运行时现读，改了设置立刻生效。"""
        config = load_settings().get("skills") or {}
        return str((config.get("weather") or {}).get("city") or "").strip()

    @staticmethod
    def _days(value: Any) -> int:
        """把模型给的 days 夹进 1~MAX_DAYS；认不出就当作只问现在。"""
        try:
            days = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(MAX_DAYS, days))

    # ---- 地名解析 -----------------------------------------------------------

    def _geocode(self, city: str) -> dict[str, Any] | None:
        """城市名 → 地点。同名城市取"名字完全对上"里人口最多的那个。"""
        cached = _GEOCODE.get(city)
        if cached is not None and time.monotonic() - cached[0] < GEOCODE_CACHE_SECONDS:
            return cached[1]
        response = _SESSION.get(
            GEOCODE_URL,
            params={"name": city, "count": 5, "language": "zh", "format": "json"},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        exact = [item for item in results if str(item.get("name", "")) == city]
        pool = exact or results
        place: dict[str, Any] | None = None
        if pool:
            place = max(pool, key=lambda item: int(item.get("population") or 0))
        _GEOCODE[city] = (time.monotonic(), place)
        return place

    # ---- 取天气并排版 -------------------------------------------------------

    def _forecast(self, place: dict[str, Any], days: int) -> str:
        response = _SESSION.get(
            FORECAST_URL,
            params={
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
                "current": (
                    "temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": days,
            },
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        response.raise_for_status()
        body = response.json()
        lines = [f"【天气】{self._place_name(place)}（数据来自 Open-Meteo）"]
        current = body.get("current") or {}
        if current:
            lines.append(
                "现在：{weather} {temp}℃（体感 {feels}℃），湿度 {humidity}%，"
                "风速 {wind}km/h，降水 {rain}mm".format(
                    weather=weather_text(current.get("weather_code")),
                    temp=_num(current.get("temperature_2m")),
                    feels=_num(current.get("apparent_temperature")),
                    humidity=_num(current.get("relative_humidity_2m")),
                    wind=_num(current.get("wind_speed_10m")),
                    rain=_num(current.get("precipitation")),
                )
            )
        daily = body.get("daily") or {}
        dates = daily.get("time") or []
        if dates:
            lines.append("预报：")
            for index, day in enumerate(dates):
                lines.append(
                    "- {day}：{weather} {low}~{high}℃，降水概率 {chance}%".format(
                        day=_day_label(day),
                        weather=weather_text(_pick(daily, "weather_code", index)),
                        low=_num(_pick(daily, "temperature_2m_min", index)),
                        high=_num(_pick(daily, "temperature_2m_max", index)),
                        chance=_num(_pick(daily, "precipitation_probability_max", index)),
                    )
                )
        lines.append("以上为预报数据，说话时可以带上具体数字，但别把它说成绝对准确。")
        return "\n".join(lines)

    @staticmethod
    def _place_name(place: dict[str, Any]) -> str:
        """「上海（上海市，中国）」这样的地名，缺哪段就少哪段。"""
        name = str(place.get("name") or "未知地点")
        parts = [str(place.get("admin1") or ""), str(place.get("country") or "")]
        detail = "，".join(part for part in parts if part)
        return f"{name}（{detail}）" if detail else name


def _pick(series: dict[str, Any], key: str, index: int) -> Any:
    values = series.get(key) or []
    return values[index] if index < len(values) else None


def _num(value: Any) -> str:
    """数字统一成最多一位小数，``None`` 显示为「—」。"""
    try:
        return f"{float(value):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "—"


def _day_label(day: str) -> str:
    """``2026-09-26`` → ``09-26 周六``；解析不出就原样返回。"""
    try:
        when = datetime.strptime(str(day), "%Y-%m-%d")
    except ValueError:
        return str(day)
    return f"{when.strftime('%m-%d')} 周{_WEEKDAYS[when.weekday()]}"
