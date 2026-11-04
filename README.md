# AWG Telegram Bot

## Установка

```bash
wget -O install_amneziawg.sh https://github.com/bivlked/amneziawg-installer/releases/latest/download/install_amneziawg.sh
```

- sudo bash install_amneziawg.sh --preset=mobile
- git clone https://github.com/rum26/awg-bot
- cd awg-bot
- python -m venv venv
- source venv/bin/activate
- pip install -r requirements.txt

## Настройка

- USER_ID — ваш Telegram ID @userinfobot
- TOKEN — токен бота от @BotFather

```bash
sudo chmod +x /opt/awg-bot/scripts/add_user.sh
sudo chmod +x /opt/awg-bot/scripts/remove_user.sh
sudo chmod +x /opt/awg-bot/scripts/stats.sh
```

## Сервис

```commandline
sudo nano /etc/systemd/system/awg-bot.service
```

```bash
[Unit]
Description=AmneziaWG Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/awg-bot
EnvironmentFile=/opt/awg-bot/.env
ExecStart=/opt/awg-bot/venv/bin/python /opt/awg-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable awg-bot
sudo systemctl start awg-bot
sudo systemctl status awg-bot
sudo systemctl stop awg-bot
sudo systemctl disable awg-bot
```
## Управление клиентами

```bash
sudo bash /root/awg/manage_amneziawg.sh add
sudo bash /root/awg/manage_amneziawg.sh list
sudo bash /root/awg/manage_amneziawg.sh remove
```