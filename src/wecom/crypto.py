"""
企业微信消息加解密

实现企业微信官方消息加解密标准（用于回调 URL 验证和消息收发）。
官方文档: https://developer.work.weixin.qq.com/document/path/90968

算法:
- 加密: AES-256-CBC + PKCS7 padding
- 签名: SHA1(token, timestamp, nonce, msg) — 先排序再拼接

加密前明文格式:
    random(16字节) + msg_len(4字节大端) + msg(UTF-8) + receive_id

注意: AES 的 IV 取密钥前 16 字节（官方约定，不是全零）。
"""
import base64
import hashlib
import random
import struct
import time

from Crypto.Cipher import AES


class WeComCrypto:
    """企业微信消息加解密器（单例使用，无状态）。"""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        self.token = token
        self.receive_id = receive_id
        # EncodingAESKey 是 43 位 base64 字符串，补 '=' 凑成 44 位后解码得到 32 字节密钥
        if len(encoding_aes_key) != 43:
            raise ValueError("EncodingAESKey 长度必须为 43 位")
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        self._iv = self.aes_key[:16]

    # ── 加密 / 解密 ──

    def _encrypt(self, plaintext: str) -> str:
        """加密明文消息 → base64 密文。"""
        random_bytes = bytes(random.randint(0, 255) for _ in range(16))
        msg = plaintext.encode("utf-8")
        length = struct.pack("!I", len(msg))
        content = random_bytes + length + msg + self.receive_id.encode("utf-8")
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self._iv)
        encrypted = cipher.encrypt(self._pkcs7_pad(content))
        return base64.b64encode(encrypted).decode("utf-8")

    def _decrypt(self, encrypted: str) -> str:
        """解密 base64 密文 → 明文消息。校验 receive_id 是否匹配。"""
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self._iv)
        content = self._pkcs7_unpad(cipher.decrypt(base64.b64decode(encrypted)))
        msg_len = struct.unpack("!I", content[16:20])[0]
        msg = content[20:20 + msg_len].decode("utf-8")
        got_receive_id = content[20 + msg_len:].decode("utf-8")
        if got_receive_id != self.receive_id:
            raise ValueError(
                f"receive_id 校验失败: 期望 {self.receive_id}，实际 {got_receive_id}"
            )
        return msg

    # ── 签名 ──

    def _signature(self, timestamp: str, nonce: str, msg: str) -> str:
        """计算 SHA1 签名: 将 token/timestamp/nonce/msg 排序后拼接再哈希。"""
        items = sorted([self.token, timestamp, nonce, msg])
        return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()

    def verify_signature(self, signature: str, timestamp: str, nonce: str, msg: str) -> bool:
        """校验签名是否一致（安全比较，防时序攻击）。"""
        expected = self._signature(timestamp, nonce, msg)
        if len(expected) != len(signature):
            return False
        return sum(a != b for a, b in zip(expected, signature)) == 0

    # ── 对外接口 ──

    def decrypt_msg(self, encrypted: str, timestamp: str, nonce: str, signature: str) -> str:
        """校验签名并解密，返回明文 XML（供回调使用）。"""
        if not self.verify_signature(signature, timestamp, nonce, encrypted):
            raise ValueError("签名校验失败，可能被篡改")
        return self._decrypt(encrypted)

    def decrypt_echostr(self, echostr: str) -> str:
        """解密 URL 验证的 echostr（不校验签名，供未认证企微使用）。

        未认证企微的验证 GET 可能携带密文 echostr 但无签名。
        若 echostr 本身是明文（非密文），解密会抛异常，由调用方捕获后直接回显原文。
        """
        return self._decrypt(echostr)

    def encrypt_reply(self, reply_xml: str) -> dict:
        """加密回复 XML，返回 (encrypt, signature, timestamp, nonce) 四元组。

        用于被动回复（方式A）。当前项目用方式B（异步主动回复），此方法保留备用。
        """
        timestamp = str(int(time.time()))
        nonce = str(random.randint(100000000, 999999999))
        encrypted = self._encrypt(reply_xml)
        signature = self._signature(timestamp, nonce, encrypted)
        return {
            "encrypt": encrypted,
            "msg_signature": signature,
            "timestamp": timestamp,
            "nonce": nonce,
        }

    # ── PKCS7 padding（块大小 32，官方规定）──

    @staticmethod
    def _pkcs7_pad(data: bytes) -> bytes:
        pad_len = 32 - (len(data) % 32)
        return data + bytes([pad_len]) * pad_len

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        if not data:
            raise ValueError("空数据")
        pad_len = data[-1]
        if pad_len < 1 or pad_len > 32:
            raise ValueError("无效的 padding 长度")
        return data[:-pad_len]
