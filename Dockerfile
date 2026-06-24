FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

# Install tectonic (LaTeX engine for CV rendering, ~35 MB binary)
# x86_64-unknown-linux-musl is statically linked — no extra system libs needed.
RUN curl -fsSL \
    "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-unknown-linux-musl.tar.gz" \
    | tar xz -C /usr/local/bin/ && chmod +x /usr/local/bin/tectonic

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-download tectonic LaTeX packages while the build still has internet access.
# This caches ~50 MB of TeX packages in /root/.cache/Tectonic so the first
# application CV render doesn't need to hit the network at runtime.
# The "|| true" prevents a failed download from breaking the build.
RUN tectonic app/cv/warmup.tex || true

CMD ["python", "-m", "app.main"]
