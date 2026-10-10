# 单用户前端

React + TypeScript + Vite，样式使用普通 CSS。所有数据通过 `/api` 读取后端，不在浏览器保存身份、密钥或简报副本。导航使用 URL hash，刷新页面仍会停留在原来的页面。

## 日常使用

首次构建：

需要 Node.js 22.12 或更高版本。当前电脑已经安装了 Node.js 22，依赖和构建产物也已准备好，可以直接启动后端。

```powershell
cd D:\NewsAgent\frontend
npm.cmd install
npm.cmd run build
cd ..
.\.venv\Scripts\python.exe -m backend
```

浏览器打开 `http://127.0.0.1:8000`。构建后，日常只需启动后端；前端修改后重新执行 `npm.cmd run build`。`frontend/dist` 是构建产物，不提交 Git；换电脑后执行 `npm.cmd ci` 和构建即可。

后端继续按已有规则加载项目 `.env`。前端没有复制这些环境变量，QQ 邮箱授权码和模型 API Key 不会打包到浏览器。

## 页面功能

- **我的简报**：最新简报、手动生成、本次额外要求、后台进度、订阅摘要、最近任务与邮件结果。
- **历史简报**：从后端分页读取归档，选择后加载正文，支持 Markdown 表格和下载。没有虚构的历史数据或全文搜索。
- **订阅设置**：编辑身份和长期偏好，设置北京时间的每日生成时间，启用/暂停订阅，查看 QQ 邮件配置状态与设置说明。

首次没有资料时引导到设置页。后端启用了长期记忆且资料不完整时，生成按钮不可用。保存资料和偏好仍使用现有 Markdown 记忆文件。

生成任务提交后，前端每 1.5 秒查询后端状态；空闲时每 10 秒刷新。重新打开页面后继续读取持久化任务状态。简报生成和邮件发送分别显示，邮件失败可以重试，不会重新调用模型生成正文。邮件结果不确定时，页面提醒先检查邮箱，使用「确认未收到，重发」按钮。

页面支持手机布局、键盘导航、焦点提示和减少动画设置。Markdown 禁用原始 HTML，外部链接在新标签页打开；远程图片显示为链接，避免打开简报时自动请求第三方图片。

## 开发模式

启动后端后，在另一个终端运行：

```powershell
cd D:\NewsAgent\frontend
npm.cmd run dev
```

访问 `http://127.0.0.1:5173`，Vite 将 `/api` 代理到 `http://127.0.0.1:8000`。后端端口改变时，修改 `frontend/vite.config.ts` 的代理地址。`npm run preview` 仅预览构建文件，不提供 API 代理；完整使用请通过 FastAPI 的 8000 端口访问。

```powershell
npm.cmd test
npm.cmd run build
```

主要文件：

| 文件 | 用途 |
| --- | --- |
| `frontend/src/App.tsx` | 导航、生成、任务记录与历史列表 |
| `frontend/src/Settings.tsx` | 身份/偏好与订阅表单、邮件状态 |
| `frontend/src/BriefReader.tsx` | 按需读取、安全渲染、下载正文 |
| `frontend/src/api.ts` | 请求、接口错误与类型映射 |
| `frontend/src/useDashboard.ts` | 状态刷新、轮询及卸载取消请求 |
| `frontend/src/styles.css` | 桌面/手机布局与视觉样式 |
| `backend/frontend.py` | 提供构建后的 HTML 与公开静态资源 |

当前定位仍是本地单用户：没有登录、多用户隔离、删除简报和通知浏览器功能。后端默认监听本机地址。
