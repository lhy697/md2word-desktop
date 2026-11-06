"""Per-user lock owns all writers; a local pipe carries activation only."""
import hashlib

from PySide6.QtCore import QObject, QLockFile, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..domain.models import failure


class SingleInstance(QObject):
    activated = Signal()

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.lock = QLockFile(str(paths.root / "application.lock"))
        self.lock.setStaleLockTime(0)  # PID/process checks remain; no age-based eviction.
        identity = hashlib.sha256(str(paths.root).casefold().encode("utf-8")).hexdigest()[:32]
        self.name = "md2word-" + identity
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.setMaxPendingConnections(4)
        self.server.newConnection.connect(self._connected)
        self.primary = False

    def acquire(self):
        if self.lock.tryLock(0):
            self.primary = True
            QLocalServer.removeServer(self.name)
            if not self.server.listen(self.name):
                self.lock.unlock()
                self.primary = False
                raise failure("INSTANCE_LISTEN_FAILED", "无法建立本地窗口通信。", "关闭现有窗口后重试。", "storage")
            return True
        if self.lock.error() != QLockFile.LockError.LockFailedError:
            raise failure("INSTANCE_LOCK_FAILED", "无法锁定用户数据目录。", "检查目录权限后重试。", "storage")
        # The first process may still be setting up its named pipe.
        socket = QLocalSocket(self)
        for _ in range(4):
            socket.connectToServer(self.name)
            if socket.waitForConnected(250):
                socket.write(b"activate\n")
                socket.waitForBytesWritten(250)
                socket.disconnectFromServer()
                socket.deleteLater()
                return False
            socket.abort()
        socket.deleteLater()
        raise failure("INSTANCE_BUSY", "已有窗口正在启动或结束任务，未开启第二个写入者。", "稍候再次启动。", "storage")

    def _connected(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            # The connection itself is activation; never accept paths or document data.
            socket.abort()
            socket.deleteLater()
            self.activated.emit()

    def close(self):
        if self.primary:
            self.server.close()
            self.lock.unlock()
            self.primary = False
