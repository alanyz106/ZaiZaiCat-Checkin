"""
Ole 签到接口
"""
import requests


class OleAPI:
    """Ole 签到相关 API"""

    def __init__(self, session_id: str, device_name: str, unique: str,
                 ole_wx_open_id: str, shop_code: str = "205368",
                 city_id: str = "c_region_11122", user_agent: str = None,
                 app_version: str = "1.11.4", timeout: int = 20):
        """
        初始化 API

        :param session_id: 会话 ID
        :param device_name: 设备名称（手机号）
        :param unique: 唯一标识
        :param ole_wx_open_id: 微信 openid
        :param shop_code: 门店编码
        :param city_id: 城市 ID
        :param user_agent: 用户代理字符串
        :param app_version: 小程序版本号（抓包实测 1.11.4，脚本旧默认 1.10.17）
        :param timeout: 单次请求超时（秒）
        """
        self.base_url = "https://ole-app.crvole.com.cn/vgdt_app_api/v1"
        self.session_id = session_id
        self.device_name = device_name
        self.unique = unique
        self.ole_wx_open_id = ole_wx_open_id
        self.shop_code = shop_code
        self.city_id = city_id
        self.app_version = app_version
        self.timeout = timeout
        self.user_agent = user_agent or 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) UnifiedPCMacWechat(0xf26405f0) XWEB/13910'

    def _get_headers(self):
        """获取请求头"""
        return {
            'User-Agent': self.user_agent,
            'Content-Type': 'application/json',
            'Tenant-Channel': 'OLE',
            'cityId': self.city_id,
            'oleWxOpenId': self.ole_wx_open_id,
            'Tenant': 'VGDT',
            'sessionId': self.session_id,
            'Device-Name': self.device_name,
            'unique': self.unique,
            'channel': 'wxmini',
            'shopCode': self.shop_code,
            'os': 'ios',
            'appVersion': self.app_version,
            'Referer': 'https://servicewechat.com/wx6c61aaeba1551439/93/page-frame.html',
            'Accept-Language': 'zh-CN,zh;q=0.9'
        }

    def sign_in(self, enter_shop_code: str = None):
        """
        签到接口

        :param enter_shop_code: 进入的门店编码，默认使用初始化时的 shop_code
        :return: 响应数据
        """
        if enter_shop_code is None:
            enter_shop_code = self.shop_code

        url = f"{self.base_url}/vgdt-fea-app-member/front_api/member_sign"
        payload = {
            "enter_shop_code": enter_shop_code
        }

        try:
            response = requests.post(url, json=payload, headers=self._get_headers(),
                                     timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"网络异常: {e}"
            }

        # 401 是网关级统一鉴权拦截：2026-10-26 实测即使请求不存在的路径、
        # 以及完全不带头，也都返回同样的 "授权失败"。所以不能靠状态码区分
        # 「session 失效」与「参数错误」，只能明确告知用户是会话问题。
        if response.status_code == 401:
            return {
                "success": False,
                "auth_failed": True,
                "error": "会话已失效（session_id 需重新抓包），Ole 无 refresh 接口",
            }

        try:
            body = response.json()
        except ValueError:
            return {
                "success": False,
                "error": f"响应非 JSON (HTTP {response.status_code}): {response.text[:120]}",
            }

        if isinstance(body, dict) and str(body.get("stateCode")) == "401":
            return {
                "success": False,
                "auth_failed": True,
                "error": "会话已失效（session_id 需重新抓包），Ole 无 refresh 接口",
            }
        return body

    def query_points(self):
        """
        查询积分

        :return: 响应数据
        """
        # 抓包中未出现积分查询接口；member_info 等猜测路径实测同样返回 401，
        # 无法区分「路径不对」与「会话失效」，故暂时保持空实现。
        return None

