# Sử dụng base image chính thức của Python 3.11
FROM python:3.11-slim

# Đặt thư mục làm việc trong container
WORKDIR /app

# Sao chép file requirements.txt và cài đặt các thư viện
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép toàn bộ code còn lại vào container
COPY . /app

# Định nghĩa lệnh mặc định khi container khởi động
CMD ["python", "main.py"]
