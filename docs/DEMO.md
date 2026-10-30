# Demo & screenshots / 演示与截图

[English README](../README.md) · [中文 README](../README.zh-CN.md)

## What you are seeing / 展示内容

These are **native SwiftUI views from this repository**, rendered at a 390 × 844 point compact layout in an isolated iOS Simulator build. Images are cropped to the app view and resized; the overview adds presentation frames and captions. They are not AI-generated interface mockups.

截图来自仓库的真实 SwiftUI 界面，通过独立模拟器中的录制程序，以 390 × 844 点的紧凑布局渲染。图片裁去模拟器外围并缩放，概览图增加了外框和说明文字，不是 AI 绘制的虚构界面。

All calendar items, courses, projects and task history are **fictional fixtures**. No personal calendar, health sample, private file, pairing code, school feed or real server address is shown. The isolated capture build does not connect to the author's running assistant. The Todo board and assistant task history are seeded display fixtures; the screenshots do not establish that a Canvas provider or language model executed those examples.

全部课程、日程、项目与任务记录为虚构示例。录制环境不连接作者正在使用的私人服务。待办板和助手记录使用展示数据，截图不代表对真实 Canvas 或模型执行结果的验收。

## Completion animation / 完成动效

![Native Today view progress animation](media/progress.gif)

The capture harness invokes the **production `DailyAgendaModel.toggle` method**, then reads the updated items after actual EventKit saves in the simulator. The shipping Today view animates the resulting progress:

| Stage | Calendar items | Completed | Progress |
| --- | --- | --- | --- |
| Initial | 5 | 0 | 0% |
| First completed item | 5 | 1 | 20% |
| Second completed item | 5 | 2 | 40% |

The recording is cropped to the Today view and encoded as a looping GIF. No microphone, human tap or cloud round-trip is shown. The trigger is automated; the EventKit completion path and SwiftUI animation are real. It is **not** an end-to-end Action Button, iCloud, APNs or navigation acceptance test.

录制程序调用正式的完成操作，真实保存模拟器内日历，再读取状态；界面根据该状态播放动效。触发来自录制程序，没有展示真人点击或语音输入，也不代表 Action Button、iCloud、推送或导航全链路已经验收。

## Full-size views / 界面原图

### Today / 今天

![Today view with fictional calendar items](media/today.png)

### Todos / 待办与项目

![Project, Canvas fixture and an undated task](media/todos.png)

### Assistant / 语音与文字

![Voice and text entry with fictional task history](media/assistant.png)

## Verification / 验证

The isolated capture build compiled successfully. The completion recording finished with **2 completed of 5**, without a calendar-operation error. Only documentation and media are added to the public product; no screenshot-mode authentication bypass or fixture data is added to the shipping client.

独立录制构建成功，完成操作最终返回 **2/5** 且没有日历操作错误。公开产品只增加文档和素材，不加入演示认证逻辑或示例任务。代码、运行行为与新用户实机验收的区别见 [VERIFICATION.md](VERIFICATION.md)。
