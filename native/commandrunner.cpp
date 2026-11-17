#include "commandrunner.h"

#include <QProcess>
#include <QTimer>

#include <csignal>
#include <unistd.h>

void CommandRunner::setTimeoutMs(int timeoutMs)
{
    if (m_timeoutMs == timeoutMs)
        return;
    m_timeoutMs = timeoutMs;
    emit timeoutMsChanged();
}

void CommandRunner::run(const QString &command)
{
    auto *process = new QProcess(this);
    QTimer *timer = nullptr;
    if (m_timeoutMs > 0) {
        timer = new QTimer(process);
        timer->setSingleShot(true);
        // Its own process group, so a timeout also stops the helper's
        // children (the CLI, Claude), not only /bin/sh.
        process->setChildProcessModifier([] { ::setpgid(0, 0); });
    }
    const auto complete = [this, process, timer, command](int exitCode, const QString &error = {}) {
        // Disconnect before notifying consumers, which may start another command
        // or destroy the runner from their finished handler.
        process->disconnect(this);
        if (timer)
            timer->stop();
        const QString standardOutput = QString::fromUtf8(process->readAllStandardOutput());
        QString standardError = QString::fromUtf8(process->readAllStandardError());
        if (!error.isEmpty())
            standardError = standardError.isEmpty() ? error : standardError.trimmed() + '\n' + error;
        process->setParent(nullptr);
        process->deleteLater();
        emit finished(command, exitCode, standardOutput, standardError);
    };

    connect(process, &QProcess::finished, this,
            [complete](int exitCode, QProcess::ExitStatus status) {
                complete(status == QProcess::NormalExit ? exitCode : -1);
            });
    connect(process, &QProcess::errorOccurred, this,
            [process, complete](QProcess::ProcessError error) {
                // A crash also emits QProcess::finished. Only startup failure
                // needs completion here, because it has no finished signal.
                if (error == QProcess::FailedToStart)
                    complete(-1, process->errorString());
            });
    if (timer) {
        const int timeoutMs = m_timeoutMs;
        connect(timer, &QTimer::timeout, this, [process, complete, timeoutMs] {
            const qint64 pid = process->processId();
            if (pid > 0)
                ::kill(-static_cast<pid_t>(pid), SIGKILL);
            complete(-1, QStringLiteral("timed out after %1 s").arg(timeoutMs / 1000.0));
        });
        timer->start(timeoutMs);
    }
    process->start(QStringLiteral("/bin/sh"), {QStringLiteral("-c"), command});
}
