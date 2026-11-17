#pragma once

#include <QObject>
#include <QString>

class CommandRunner : public QObject
{
    Q_OBJECT
    // Milliseconds a command may run before it is killed; 0 = no limit.
    Q_PROPERTY(int timeoutMs READ timeoutMs WRITE setTimeoutMs NOTIFY timeoutMsChanged)

public:
    explicit CommandRunner(QObject *parent = nullptr) : QObject(parent) {}

    int timeoutMs() const { return m_timeoutMs; }
    void setTimeoutMs(int timeoutMs);

    Q_INVOKABLE void run(const QString &command);

signals:
    void finished(const QString &command, int exitCode,
                  const QString &standardOutput, const QString &standardError);
    void timeoutMsChanged();

private:
    int m_timeoutMs = 0;
};
