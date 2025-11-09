# Sử dụng Python 3.11 làm base image
FROM python:3.11-slim

# Tạo thư mục làm việc
WORKDIR /app

# Copy toàn bộ project vào container
COPY . .

# Cài đặt các thư viện cần thiết
RUN pip install --no-cache-dir -r requirements.txt

# ✅ Lệnh khởi chạy chính thức (Render yêu cầu có CMD)
CMD ["python", "main.py"]
