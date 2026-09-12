#!/bin/bash
# 一键部署共享 MCP 服务器。用法：./deploy_mcp.sh
#
# 做四件事：装 vercel CLI、生成访问令牌、把 .env 里的连接字符串传给 Vercel、部署。
# 数据库用的是连接字符串，不需要任何 Neon 账号。
set -e
cd "$(dirname "$0")"

TOKENS=config/mcp_tokens.json

echo "==> 1/4 检查 vercel 命令"
if ! command -v vercel >/dev/null; then
  echo "    没装，正在安装..."
  npm i -g vercel
fi

echo "==> 2/4 访问令牌"
if [ ! -f "$TOKENS" ]; then
  python3 - "$TOKENS" <<'PY'
import json, secrets, sys
path = sys.argv[1]
data = {secrets.token_hex(24): {"name": "yi", "secrets": True}}
with open(path, "w") as f:
    json.dump(data, f, indent=2)
PY
  chmod 600 "$TOKENS"
  echo "    已生成第一个令牌 -> $TOKENS"
else
  echo "    已存在 -> $TOKENS（要加人就编辑这个文件后重跑）"
fi

# .env 是 shell 格式（export KEY=...），source 进来即可
set -a; . ./.env; set +a

echo "==> 3/4 上传配置到 Vercel"
vercel link
for VAR in DATABASE_URL SECRETS_KEY; do
  vercel env rm "$VAR" production --yes >/dev/null 2>&1 || true
  printf '%s' "${!VAR}" | vercel env add "$VAR" production
done
vercel env rm KDB_TOKENS production --yes >/dev/null 2>&1 || true
tr -d '\n' < "$TOKENS" | vercel env add KDB_TOKENS production

echo "==> 4/4 部署"
vercel deploy --prod

echo
echo "======================================================"
echo "好了。上面那个 https://... 网址记下来，然后："
echo
python3 - "$TOKENS" <<'PY'
import json, sys
tok = next(iter(json.load(open(sys.argv[1]))))
print(f"  你的连接器网址 = 上面那个网址 + /mcp/{tok}")
PY
echo
echo "在 Claude 里：设置 -> 连接器 -> 添加自定义连接器 -> 粘贴上面这条完整网址。"
echo "验证：浏览器打开 <网址>/health，看到 {\"ok\": true} 就是通了。"
echo "======================================================"
