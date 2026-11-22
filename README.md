# Video Shuffler

Cắt video thành các đoạn ngẫu nhiên 1–2 giây, xáo trộn, rồi ghép thành nhiều clip ngắn (mặc định 15 giây, số clip tối đa theo thời lượng video). Clip đầu ra không có audio.

## Chạy

Bấm đúp `start.command` trong Finder, hoặc:

```bash
.venv/bin/vshuffle-ui                                  # app có giao diện
.venv/bin/vshuffle a.mov b.mov --length 15 [--clips N] [--quality gpu|cpu_quiet|cpu_max]
```

- Kéo thả nhiều video (hoặc cả thư mục) vào cửa sổ. Video được xếp hàng và render lần lượt từng cái.
- Số clip mỗi video mặc định là tối đa mà không lặp cảnh (thời lượng video ÷ thời lượng clip), có thể chỉnh riêng từng video.
- Chọn nhiều clip hơn mức đó thì các clip vượt (tên file `*_lap.mp4`) dùng lại cảnh. Các clip đầu vẫn không lặp cảnh. Clip lặp ưu tiên phần video chưa clip nào dùng, sau đó tới phần ít được dùng nhất, cắt ở vị trí mới và tránh lấy nhiều cảnh từ cùng một clip cũ, nên các clip lặp khác nhau và khác các clip trước. Mỗi video tối đa 99 clip.
- Clip của mọi video được lưu chung trong `~/Movies/Video Shuffler/` (đổi được trong app), tên dạng `IMG_4821_clip01.mp4`. Render lại cùng video thì tên thành `IMG_4821_2_clip01.mp4`, không đè file cũ. Kế hoạch cắt của mỗi lần render (để tra cứu seed) nằm trong thư mục ẩn `.plans/`.
- Cài đặt được lưu lại giữa các lần mở app (`~/Library/Application Support/VideoShuffler/settings.json`).

## Thời lượng riêng cho từng video

Thời lượng clip và độ dài đoạn cắt ở thanh bên là mặc định cho mọi video. Bấm nhãn **⏱** trên một video để đặt riêng (ví dụ video này clip 8s, đoạn 0,5–1s). Hộp thoại cho biết ngay video ra được tối đa bao nhiêu clip không lặp, và báo lỗi nếu thời lượng clip không chia được thành các đoạn trong khoảng đã chọn. Video đang báo lỗi sẽ không được đưa vào hàng đợi render.

## Watermark

- Chữ và/hoặc logo (PNG nền trong suốt), đứng yên ở 1 trong 9 vị trí. Chỉnh được font, cỡ, màu, độ đậm nhạt, bóng đổ, nền mờ, khoảng cách tới mép. Có ảnh xem trước trên khung hình thật của video.
- Bật ở thanh bên để áp dụng mặc định cho mọi clip. Nhãn **WM** trên mỗi video cho phép: dùng mặc định, tắt, hoặc watermark riêng cho cả video; và đặt riêng (theo video / tắt / riêng) cho từng clip.
- Kích thước tính theo % cạnh ngắn của video nên video dọc và ngang nhìn cân đối như nhau. Với video HDR của iPhone, màu watermark được chuyển sang HDR (trắng = 203 nit) để không bị chói hay lệch màu.
- Watermark được chèn trong cùng lần encode, không làm giảm chất lượng thêm.

## Chế độ render

| Chế độ | Encoder | CPU (4K HDR, 1 clip 15s, M2) |
|---|---|---|
| Chip Mac (mặc định) | VideoToolbox HEVC q65 / H.264 q70 | ~4s, thời gian ~10s |
| CPU êm | x265 CRF18 / x264 CRF17, chỉ lõi tiết kiệm điện | chậm (~8 phút) |
| CPU tối đa | x265 CRF18 / x264 CRF17, toàn bộ lõi (như bản 0.1) | ~300s, thời gian ~45s |

Theo VMAF, chế độ Chip Mac cho chất lượng bằng hoặc cao hơn x265 CRF18 / x264 CRF17. Mọi chế độ đều giải mã bằng chip Mac, giữ độ phân giải, fps, 10-bit và màu HDR của iPhone, cắt chính xác đến từng frame.

Yêu cầu: `brew install ffmpeg`, Python ≥ 3.10.
