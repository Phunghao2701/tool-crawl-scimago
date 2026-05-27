FROM python:3.11-slim

WORKDIR /app

# Cài đặt các gói hệ thống cần thiết để build thư viện nếu có
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir openpyxl

# Sao chép mã nguồn vào container
COPY . .

# Giữ container chạy ở chế độ nền để người dùng thực thi các câu lệnh ETL
CMD ["tail", "-f", "/dev/null"]
