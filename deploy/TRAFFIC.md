# 查看网站访问情况

本文介绍如何为部署后的本站配置独立访问日志和近一周统计。实际路径、轮转规则与定时器状态以服务器配置为准；修改仓库文件后，还需在服务器安装配置并验证。

用 GoAccess 将日志生成 HTML 图形报表。先按需生成并下载查看，不把报表放到公开网站，也不额外开放统计端口。

## 指标含义

| 指标 | 适合观察 | 限制 |
| --- | --- | --- |
| 每日估算独立访客 | 访问趋势 | 默认按 IP、日期、User-Agent 组合去重，不等于真实人数；同网同浏览器可能合并，换网络或浏览器可能重复 |
| 每小时请求分布 | 哪些时段请求集中 | 前端每分钟自动刷新，登录后还有收藏接口请求；请求数不是打开网页次数 |
| 浏览器与操作系统 | 浏览器与系统分布 | 来源是请求头，不能可靠识别某位同学 |
| 路径和状态码 | 热门请求、404 和服务器错误 | 一次页面访问会产生 HTML、JS、CSS、接口等多个请求 |

当前通知详情从前端已加载列表打开，没有单独的阅读请求。日志不能证明某位同学已读，也不能提供准确实时在线人数；这些需要应用内统计。

## 逐步启用

1. 在 Ubuntu 服务器安装工具：

    ```bash
    sudo apt-get update
    sudo apt-get install -y goaccess
    ```

2. 检查 `/etc/nginx/sites-available/class-notice`，先备份，并确认本站 `server` 块内包含以下两行。随附模板已有这些配置，勿重复添加；已有线上配置应逐项合并修改，不用模板直接覆盖。首次使用时先执行 `sudo install -d -m 750 -o root -g adm /var/log/nginx/class-notice` 创建访问日志目录。

    ```nginx
    access_log /var/log/nginx/class-notice/access.log combined;
    error_log /var/log/nginx/class-notice.error.log warn;
    ```

3. 执行 `sudo nginx -t`，成功后执行 `sudo systemctl reload nginx`。刷新网站，再确认独立日志有新记录。不要把含 IP 和请求路径的整份日志贴到公开聊天或网页。

4. 生成仅 root 可读的简体中文 HTML 报表。首次先安装并生成中文 locale；不会切换系统默认语言：

    ```bash
    sudo apt-get install -y locales
    sudo locale-gen zh_CN.UTF-8

    sudo install -d -m 700 /root/class-notice-reports
    sudo bash -o pipefail -c 'umask 077; zcat -f /var/log/nginx/class-notice/access.log* | env -u LC_ALL LANG=zh_CN.UTF-8 LANGUAGE=zh_CN LC_MESSAGES=zh_CN.UTF-8 LC_TIME=C goaccess --log-format=COMBINED --ignore-crawlers --no-query-string --html-report-title="班级通知网站 · 近一周访问统计" -o /root/class-notice-reports/traffic-zh.html'
    ```

不要使用 `LC_ALL=C`，它会将消息语言强制设为英文。这里只保留 `LC_TIME=C`，确保 Nginx 日志中的英文月份可正常解析；报告消息语言使用简体中文。报告能否显示中文取决于安装版本是否提供 `zh_CN` 翻译，以及 locale 是否成功生成，需以实际生成结果确认。

报告是生成时的快照，下次需要重新生成。先检查 `goaccess --version` 和运行结果；选项不支持时按安装版本调整。`--ignore-crawlers` 只能过滤已识别爬虫，不能保证剩余请求来自真人。

5. 通过受控的 SSH/SFTP 通道或云服务商文件管理下载 `/root/class-notice-reports/traffic-zh.html`，在自己的浏览器打开。报告目录仅 root 可读，使用有权限的管理入口，勿为了下载改成公开权限。报告可能包含访问 IP、URL 和来源，只供维护者查看，不放入 `static` 或公开 Nginx 目录。

## 时间范围

独立统计从启用上述日志配置后开始。如果服务器同时运行多个网站，旧日志又未记录站点身份，就不能可靠拆出本站历史访问量。

新路径 /var/log/nginx/class-notice/access.log 与其数字轮转归档共同覆盖保留范围；生成命令会合并活动文件、未压缩归档和 .gz 文件。初次启用没有一周历史时，只显示已经记录的部分。

## 只保留本站最近一周

先检查服务器 `/etc/logrotate.d/nginx` 的实际规则。常见配置匹配 `/var/log/nginx/*.log`，本站使用子目录 `/var/log/nginx/class-notice/access.log` 避免被该模式重复匹配；若已有规则会递归匹配子目录，需先调整重复部分。其他站点的轮转规则保持不变。

1. 先备份本站 Nginx 配置。新目录使用 `root:adm`、`0750`；若从其他路径迁移，已有当前日志及数字后缀归档应迁移而非清空，目标文件存在则停止，不能覆盖。
2. 如需迁移，只把本站 `access_log` 路径替换为新路径；`nginx -t` 通过后 reload。等旧 worker 退出，再用带实际 `Host` 的本机请求验证新日志确实新增记录。
3. 安装随附 `deploy/class-notice.logrotate` 为 `/etc/logrotate.d/class-notice`，属主 `root:root`、`0644`。若同名规则已经存在，先检查，不直接覆盖。
4. 执行 `sudo logrotate --debug /etc/logrotate.conf`，确认没有重复条目、权限或语法错误；debug 不轮转、不清理。检查 `systemctl status logrotate.timer`；若系统已有有效的 timer 或 cron 调度，不重复创建。不要强制轮转整份全局配置。

策略是 `daily + rotate 6 + maxage 7 + ifempty`：活动文件加六份每日归档，按每天的轮转批次保留近一周，不是逐条精确 168 小时删除。`ifempty` 让无访问的日期也能轮转；`maxage` 的年龄检查只在发生轮转时进行。只针对本站访问日志，现有错误日志和其他站点保持原规则。

若更改日志路径，也要同步更新报表生成命令。已生成或下载的 HTML 是独立快照，不会因日志轮转自动删除；需要时手动删除旧报表。

## 参考文档

- GoAccess 官方手册：https://goaccess.io/man
- Nginx 日志模块：https://nginx.org/en/docs/http/ngx_http_log_module.html
