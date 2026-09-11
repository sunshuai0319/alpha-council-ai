FROM registry.cn-hongkong.aliyuncs.com/jac0625/node:22-alpine-arm64

WORKDIR /app

# NEXT_PUBLIC_* 由 Next 在**构建时**内联进客户端 bundle，运行时再设同名环境变量
# 是无效的 —— 之前镜像里因此硬编码了 http://localhost:8000/api，别的机器访问时
# 浏览器会去请求它自己的 localhost。所以这些值必须在这里以 build args 传入。
#
# 代价：改了这些值要重新构建镜像（docker compose build web）。
ARG NEXT_PUBLIC_API_BASE_URL=/api
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=
ARG NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
ARG NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
ARG NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL=/dashboard
ARG NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_REDIRECT_URL=/dashboard

ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY \
    NEXT_PUBLIC_CLERK_SIGN_IN_URL=$NEXT_PUBLIC_CLERK_SIGN_IN_URL \
    NEXT_PUBLIC_CLERK_SIGN_UP_URL=$NEXT_PUBLIC_CLERK_SIGN_UP_URL \
    NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL=$NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL \
    NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_REDIRECT_URL=$NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_REDIRECT_URL

# 国内构建：npm 走 npmmirror（package-lock 的 resolved 本来也指向它，这里显式
# 声明，构建期若需补下载也走镜像，不回落 registry.npmjs.org）。
ENV NPM_CONFIG_REGISTRY=https://registry.npmmirror.com

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

EXPOSE 3000
CMD ["npm", "run", "start"]
