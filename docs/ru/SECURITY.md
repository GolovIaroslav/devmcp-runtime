# Безопасность runtime

Для beta используйте Linux+bwrap. Runtime нормализует относительные пути,
отвергает symlink escape, фильтрует чувствительное окружение и запускает
argv-задачи через `shell=False` по умолчанию.

Unsafe host mode включается явно и отображается как
`SANDBOX: UNSAFE HOST MODE`; тихого fallback нет. Не открывайте Docker/Podman
сокеты. UI работает только на loopback и применяет Host/Origin, CSRF, CSP,
SameSite и redaction секретов.
