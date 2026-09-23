# COIN_API

Tool batch Excel cho flow Coin register, dựng theo mẫu gọn từ `Neppi_Pay`.

Project gọi API thật tới `coin_base_url` trong `config.json`; không tạo kết quả giả.

## Excel Columns

Workbook có 3 sheet chính:

```text
Accounts
Success
Proxies
```

Sheet input mặc định: `Accounts`

```text
phone
password
katakana_last_name
katakana_first_name
date_of_birth
pin
status
step_status
error_details
proxy_used
proxy_id
coin_id
customer_status
pcard_status
gift_code_1
gift_code_2
gift1_status
gift2_status
card_number
card_name
card_expiry
security_code
card_url
registered_at
```

Sheet `Success` dùng cùng headers với `Accounts` và tự lưu account đăng ký thành
công. Sheet `Proxies` dùng headers:

```text
proxy
status
proxy_id
```

Proxy `ACTIVE` hoặc status trống sẽ được lấy để chạy. Khi một worker đang dùng
proxy, proxy đó bị khóa trong bộ nhớ; chạy xong thì mở khóa để account khác có
thể tái sử dụng. Dòng proxy mẫu trong file template để `DISABLED`; đổi thành
`ACTIVE` sau khi thay bằng proxy thật.

## Status

`status` là kết luận tổng:

```text
PENDING
PROCESSING
SUCCESS
FAILED
RETRY
FAIL_NO_RETRY
```

`step_status` cho biết đang hoặc đã dừng ở bước nào:

```text
SMS_REQUEST_OK
SMS_VERIFY_OK
PIN_CHECK_OK
REGISTER_OK
GIFT_CODE_1_OK
GIFT_CODE_2_OK
PCARD_PIN_VERIFY_OK
PCARD_WEBVIEW_OTP_OK
PCARD_WEBVIEW_AUTH_OK
PCARD_CARD_INFO_OK
ACCOUNT_ACTIVE
SMS_REQUEST_FAILED
SMS_VERIFY_FAILED
PIN_CHECK_FAILED
REGISTER_FAILED
GIFT_CODE_1_FAILED
GIFT_CODE_2_FAILED
PCARD_PIN_VERIFY_FAILED
PCARD_WEBVIEW_OTP_FAILED
PCARD_WEBVIEW_LOGIN_FAILED
PCARD_WEBVIEW_AUTH_FAILED
PCARD_CARD_URL_FAILED
PCARD_CARD_HTML_FAILED
ACCOUNT_CHECK_FAILED
VALIDATION_FAILED
```

## API Flow

Theo HAR `app-analytics-services.coin_register.har`, flow đăng ký gọi các API:

```text
POST /v2/authentications/sms-new-entry/request
POST /authentications/sms-new-entry/verify
POST /authentications/pin/validity-check
POST /v3/customers/register
GET  /v2/customers/status
GET  /v4/customers?configurationMode=0
```

Sau `REGISTER_OK`, tool chạy thêm luồng gift/card:

```text
POST /transactions/gift-codes/grant                # gift_code_1
POST /transactions/gift-codes/grant                # gift_code_2
POST /authentications/pin/verify                   # lấy PCARD token
POST /authentications/pcard-webview-otp/issue      # lấy otpCode webview
POST https://web.coinplus-prepaid.jp/login
POST https://web.coinplus-prepaid.jp/authenticate
GET  https://web.coinplus-prepaid.jp/getCardNum
GET  https://prepaidcube-multi.paycierge.com/05025/card?code=...&cc=05025
```

`tocapi.coinplus.jp` dùng header app và `XSRF-TOKEN`. Webview dùng cookie
`SESSION` riêng do server cấp qua `Set-Cookie`; Paycierge lấy HTML card bằng URL
`code=...&cc=05025` từ `/getCardNum`.

Tool luôn dùng OTPBase để chốt mốc SMS trước khi request mã, rồi chờ OTP mới
của đúng số điện thoại. `pin` là `simpleAuthenticationCode` dùng cho bước PIN
check và register.

Mỗi account flow tạo header mới trước khi gọi API:

```text
x-sgc-app-version: 1.76.0
x-sgc-client-id: 00001
x-sgc-device-id: <uuid mới>
x-sgc-device-os: iOS/<random 26.x>
x-sgc-device-model: iPhone
accept-language: vi-VN;q=1.0
user-agent: Mozilla/5.0 ... Mobile/15E148 - SGCAPP
```

## Run

```bash
cd /Users/macbook/Desktop/FPT/TOOL_REG/COIN_API
python3 tools/create_template.py
python3 api_main.py --limit 1
```

GUI đơn giản:

```bash
python3 gui.py
```

## Config Notes

`screenId` và `cmsTermsOfServiceSetId` không nằm trong Excel vì chúng là cấu hình
chung của flow:

```json
{
  "coin_base_url": "https://tocapi.coinplus.jp",
  "coin_http_headers": {},
  "otpbase_api_key": "...",
  "otp_wait_timeout": 120,
  "otp_poll_interval": 2,
  "use_proxy": true,
  "proxy_failure_threshold": 3,
  "coin_screen_id": "3",
  "coin_cms_terms_of_service_set_id": "sgc-ts-4020",
  "gift_codes": []
}
```

Nếu cần override/thêm header riêng, thêm vào `coin_http_headers`.
Nếu không muốn điền gift code theo từng dòng Excel, có thể cấu hình 2 code dùng
chung trong `gift_codes`.
