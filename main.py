import sys
import os
import json
import logging
import zipfile
import bisect
import re
from pathlib import Path
from typing import List, Optional, Tuple, Union
from io import BytesIO

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QScrollArea, QLabel, QFileDialog, QMessageBox,
    QSpinBox, QLineEdit, QStatusBar, QSlider
)
from PySide6.QtCore import Qt, QTimer, QSize, Signal, QEvent, QBuffer
from PySide6.QtGui import QPixmap, QFont, QFontMetrics, QAction, QKeySequence, QImageReader, QPainter

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 配置文件路径
CONFIG_DIR = Path.home() / ".comic_reader"
CONFIG_FILE = CONFIG_DIR / "config.json"

# 默认配置
DEFAULT_CONFIG = {
    "width": 800,          # 默认图片宽度
    "progress": {}         # 路径（文件夹或压缩包） -> 上次查看的图片索引
}

# 支持的文件扩展名
SUPPORTED_EXT = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')
SUPPORTED_ARCHIVE_EXT = ('.zip', '.cbz')

def natural_sort_key(s: str) -> List:
    """自然排序键函数，将文件名中的数字部分转为整数"""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]

class ImageWidget(QWidget):
    """单个图片控件，支持从本地文件或压缩包内文件加载"""
    def __init__(self, display_path: str, original_width: int, original_height: int,
                 archive_path: Optional[str] = None, internal_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.display_path = display_path          # 用于显示的路径（如文件名或压缩包内标识）
        self.original_width = original_width
        self.original_height = original_height
        self.archive_path = archive_path          # 压缩包路径，None 表示本地文件
        self.internal_path = internal_path        # 压缩包内文件路径
        self.display_width = 800                  # 当前显示宽度，由外部设置
        self.pixmap: Optional[QPixmap] = None
        self.loaded = False
        self.current_height = 0                   # 当前控件高度（占位高度）

        # 布局：无间距，无边距
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 图片显示标签
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background-color: #2b2b2b; border: none;")
        layout.addWidget(self.image_label)

        # 状态文字标签（悬浮于图片下方，不影响布局）
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #888; font-size: 12px; background-color: rgba(0,0,0,0.5);")
        self.status_label.setFixedHeight(20)
        self.status_label.hide()  # 默认隐藏，加载/卸载时显示提示
        layout.addWidget(self.status_label)

        self.setLayout(layout)

        # 根据当前宽度计算占位高度
        self.set_display_size(self.display_width)

    def set_display_size(self, width: int):
        """设置显示宽度，并重新计算当前控件高度（占位高度）"""
        if width == self.display_width:
            return
        self.display_width = width
        # 计算等比例高度
        if self.original_width > 0 and self.original_height > 0:
            self.current_height = int(width * self.original_height / self.original_width)
        else:
            self.current_height = 0
        self.setFixedHeight(self.current_height)
        # 如果已加载，重新缩放图片
        if self.loaded and self.pixmap:
            self.set_pixmap(self.pixmap)

    def set_pixmap(self, pixmap: QPixmap):
        """设置 pixmap，并按当前显示宽度等比例缩放，确保占位高度匹配"""
        self.pixmap = pixmap
        if not pixmap.isNull():
            # 缩放至目标宽度，使用高质量平滑变换
            scaled = pixmap.scaledToWidth(self.display_width, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
            # 确保控件高度与缩放后的图片高度一致
            new_height = scaled.height()
            if new_height != self.current_height:
                # 理论上应相等，若不等则调整控件高度（可能是浮点误差）
                self.setFixedHeight(new_height)
                self.current_height = new_height
            self.loaded = True
            self.status_label.hide()  # 加载成功隐藏状态文字
        else:
            self.image_label.clear()
            self.loaded = False
            self.set_status("加载失败")

    def set_status(self, text: str):
        """显示状态文字，2秒后自动隐藏"""
        self.status_label.setText(text)
        self.status_label.show()
        QTimer.singleShot(2000, self.status_label.hide)

    def clear_image(self):
        """卸载图片，释放内存"""
        if self.loaded:
            self.image_label.clear()
            self.pixmap = None
            self.loaded = False
            self.set_status("已卸载")

    def load_image(self):
        """加载图片（同步）"""
        if self.loaded:
            return
        try:
            if self.archive_path and self.internal_path:
                # 从压缩包加载
                with zipfile.ZipFile(self.archive_path, 'r') as zf:
                    with zf.open(self.internal_path) as f:
                        data = f.read()
                pixmap = QPixmap()
                pixmap.loadFromData(data)
            else:
                # 从本地文件加载
                pixmap = QPixmap(self.display_path)
            if pixmap.isNull():
                raise Exception("图片加载失败")
            self.set_pixmap(pixmap)
            logging.info(f"加载图片: {self.display_path}")
        except Exception as e:
            logging.error(f"图片加载失败 {self.display_path}: {e}")
            self.set_status("加载失败")

class ComicScrollArea(QScrollArea):
    """支持虚拟滚动的滚动区域，仅创建可见的图片控件"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setStyleSheet("""
            QScrollArea { border: none; background-color: #1e1e1e; }
            QScrollBar:vertical {
                background: #2b2b2b;
                width: 14px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #ff8800;
                border-radius: 7px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #ffaa00;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background-color: #1e1e1e;")
        self.setWidget(self.content_widget)

        # 数据：每个元素为 (display_path, orig_w, orig_h, archive_path, internal_path)
        self.image_data: List[Tuple[str, int, int, Optional[str], Optional[str]]] = []
        self.image_widgets: dict[int, ImageWidget] = {}
        self.current_width = 800
        self.offsets: List[int] = []      # 每个图片顶部的y坐标
        self.total_height = 0

        self.layout_timer = QTimer()
        self.layout_timer.setSingleShot(True)
        self.layout_timer.timeout.connect(self.update_visible_widgets)

        self._updating = False
        self.verticalScrollBar().valueChanged.connect(self.on_scroll)

    def set_image_data(self, image_data: List[Tuple[str, int, int, Optional[str], Optional[str]]], width: int):
        """设置图片数据并重建布局"""
        for widget in self.image_widgets.values():
            widget.deleteLater()
        self.image_widgets.clear()
        self.image_data = image_data
        self.current_width = width
        self.rebuild_layout()
        self.content_widget.setFixedSize(self.viewport().width(), self.total_height)
        self.update_visible_widgets()

    def set_width(self, width: int, keep_position: bool = True):
        """改变显示宽度，可选保持当前阅读进度"""
        if width == self.current_width:
            return
        current_idx = None
        if keep_position and self.image_data:
            current_idx = self.get_current_index()
        self.current_width = width
        self.rebuild_layout()
        for idx, widget in self.image_widgets.items():
            widget.set_display_size(width)
            if idx < len(self.offsets):
                widget.setGeometry(0, self.offsets[idx], self.content_widget.width(), widget.height())
        self.content_widget.setFixedSize(self.viewport().width(), self.total_height)
        self.update_visible_widgets()
        if keep_position and current_idx is not None and current_idx < len(self.offsets):
            self.scroll_to_index(current_idx)

    def rebuild_layout(self):
        """根据当前宽度重新计算所有图片的高度和偏移量"""
        if not self.image_data:
            self.offsets = []
            self.total_height = 0
            return

        heights = []
        for _, orig_w, orig_h, _, _ in self.image_data:
            if orig_w > 0 and orig_h > 0:
                h = int(self.current_width * orig_h / orig_w)
            else:
                h = 0
            heights.append(h)

        self.offsets = [0] * len(heights)
        total = 0
        for i, h in enumerate(heights):
            self.offsets[i] = total
            total += h
        self.total_height = total

    def on_scroll(self, value):
        if not self.layout_timer.isActive():
            self.layout_timer.start(50)

    def update_visible_widgets(self):
        """根据滚动位置创建/销毁控件并加载图片"""
        if self._updating or not self.image_data:
            return
        self._updating = True
        try:
            viewport = self.viewport()
            view_height = viewport.height()
            scroll_top = self.verticalScrollBar().value()
            scroll_bottom = scroll_top + view_height
            buffer = view_height * 1.5
            start_y = max(0, scroll_top - buffer)
            end_y = min(self.total_height, scroll_bottom + buffer)

            start_idx = bisect.bisect_right(self.offsets, start_y) - 1
            if start_idx < 0:
                start_idx = 0
            end_idx = bisect.bisect_left(self.offsets, end_y) - 1
            if end_idx >= len(self.offsets):
                end_idx = len(self.offsets) - 1

            needed_indices = set(range(start_idx, end_idx + 1))

            # 移除不需要的控件
            for idx in list(self.image_widgets.keys()):
                if idx not in needed_indices:
                    self.image_widgets.pop(idx).deleteLater()

            # 创建缺失的控件
            for idx in needed_indices:
                if idx not in self.image_widgets:
                    display_path, orig_w, orig_h, archive_path, internal_path = self.image_data[idx]
                    widget = ImageWidget(display_path, orig_w, orig_h,
                                         archive_path, internal_path,
                                         self.content_widget)
                    widget.set_display_size(self.current_width)
                    widget.setGeometry(0, self.offsets[idx],
                                       self.content_widget.width(), widget.height())
                    widget.show()
                    self.image_widgets[idx] = widget
                    widget.load_image()
        finally:
            self._updating = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        new_width = self.viewport().width()
        if new_width > 0:
            self.content_widget.setFixedWidth(new_width)
            for widget in self.image_widgets.values():
                widget.setGeometry(0, widget.geometry().y(), new_width, widget.height())

    def get_current_index(self) -> int:
        """获取当前顶部可见图片的索引"""
        if not self.image_data:
            return 0
        scroll_top = self.verticalScrollBar().value()
        if self.total_height == 0:
            return 0
        idx = bisect.bisect_right(self.offsets, scroll_top) - 1
        if idx < 0:
            idx = 0
        if idx >= len(self.offsets):
            idx = len(self.offsets) - 1
        return idx

    def scroll_to_index(self, index: int) -> bool:
        """滚动到指定索引的图片顶部"""
        if index < 0 or index >= len(self.offsets):
            return False
        self.verticalScrollBar().setValue(self.offsets[index])
        self.update_visible_widgets()
        return True

class ComicReader(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("漫画阅读器")
        self.setMinimumSize(800, 600)
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; }
            QPushButton {
                background-color: #3c3c3c; color: white;
                border: 1px solid #5a5a5a; border-radius: 4px;
                padding: 6px 12px; font-size: 12px;
            }
            QPushButton:hover { background-color: #505050; border-color: #7a7a7a; }
            QPushButton:pressed { background-color: #2c2c2c; }
            QLineEdit, QSpinBox {
                background-color: #2c2c2c; color: white;
                border: 1px solid #5a5a5a; border-radius: 4px;
                padding: 4px;
            }
            QLabel { color: #cccccc; }
            QStatusBar { background-color: #2c2c2c; color: #ffcc88; }
        """)

        self.current_folder: Optional[str] = None
        self.current_archive: Optional[str] = None          # 当前压缩包路径
        self.image_data: List[Tuple[str, int, int, Optional[str], Optional[str]]] = []
        self.current_width = DEFAULT_CONFIG["width"]

        self.config = self.load_config()
        self.setup_ui()

        self.installEventFilter(self)

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setFixedHeight(50)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 5, 10, 5)
        toolbar_layout.setSpacing(10)

        self.btn_select = QPushButton("选择漫画文件夹")
        self.btn_select.clicked.connect(self.select_folder)
        toolbar_layout.addWidget(self.btn_select)

        self.btn_open_archive = QPushButton("打开压缩包 (ZIP/CBZ)")
        self.btn_open_archive.clicked.connect(self.open_archive)
        toolbar_layout.addWidget(self.btn_open_archive)


        self.btn_exit = QPushButton("退出")
        self.btn_exit.clicked.connect(self.exit_current)
        toolbar_layout.addWidget(self.btn_exit)

        width_label = QLabel("显示宽度（请在图片首页调整）:")
        toolbar_layout.addWidget(width_label)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(100, 3000)
        self.width_spin.setSingleStep(50)
        self.width_spin.setValue(self.current_width)
        self.width_spin.valueChanged.connect(self.adjust_image_width)
        toolbar_layout.addWidget(self.width_spin)

        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(100, 3000)
        self.width_slider.setSingleStep(50)
        self.width_slider.setValue(self.current_width)
        self.width_slider.valueChanged.connect(self.width_spin.setValue)
        toolbar_layout.addWidget(self.width_slider)

        toolbar_layout.addStretch()

        self.btn_first = QPushButton("首页")
        self.btn_first.clicked.connect(self.jump_to_first)
        toolbar_layout.addWidget(self.btn_first)

        self.btn_last = QPushButton("尾页")
        self.btn_last.clicked.connect(self.jump_to_last)
        toolbar_layout.addWidget(self.btn_last)

        self.page_input = QLineEdit()
        self.page_input.setPlaceholderText("页码")
        self.page_input.setFixedWidth(80)
        toolbar_layout.addWidget(self.page_input)
        self.btn_jump_page = QPushButton("跳转")
        self.btn_jump_page.clicked.connect(self.jump_by_page)
        toolbar_layout.addWidget(self.btn_jump_page)

        self.filename_input = QLineEdit()
        self.filename_input.setPlaceholderText("文件名")
        self.filename_input.setFixedWidth(150)
        toolbar_layout.addWidget(self.filename_input)
        self.btn_jump_filename = QPushButton("跳转")
        self.btn_jump_filename.clicked.connect(self.jump_by_filename)
        toolbar_layout.addWidget(self.btn_jump_filename)

        main_layout.addWidget(toolbar)

        self.scroll_area = ComicScrollArea()
        main_layout.addWidget(self.scroll_area)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("未选择漫画")
        self.status_bar.addWidget(self.status_label)

    # ---------- 配置管理 ----------
    def load_config(self):
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                config.setdefault('width', DEFAULT_CONFIG['width'])
                config.setdefault('progress', {})
                return config
            else:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                return DEFAULT_CONFIG.copy()
        except Exception as e:
            logging.error(f"配置文件加载失败: {e}")
            return DEFAULT_CONFIG.copy()

    def save_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logging.error(f"保存配置失败: {e}")

    def save_current_progress(self):
        key = self.current_archive if self.current_archive else self.current_folder
        if not key or not self.image_data:
            if key and key in self.config.get('progress', {}):
                del self.config['progress'][key]
                self.save_config()
            return
        idx = self.scroll_area.get_current_index()
        self.config['progress'][key] = idx
        self.save_config()

    def restore_progress(self, key: str) -> int:
        progress = self.config.get('progress', {})
        return progress.get(key, 0)

    # ---------- 图片尺寸获取 ----------
    def get_image_dimensions(self, file_path: str) -> Tuple[int, int]:
        reader = QImageReader(file_path)
        size = reader.size()
        if size.isValid():
            return size.width(), size.height()
        logging.warning(f"无法获取图片尺寸: {file_path}")
        return 0, 0

    def get_image_dimensions_from_data(self, data: bytes) -> Tuple[int, int]:
        """从图片二进制数据获取尺寸"""
        buffer = QBuffer()
        buffer.setData(data)
        buffer.open(QBuffer.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        if size.isValid():
            return size.width(), size.height()
        return 0, 0

    # ---------- 加载漫画 ----------
    def load_folder(self, folder: str):
        """加载文件夹中的图片"""
        image_paths = {}
        for ext in SUPPORTED_EXT:
            for p in Path(folder).glob(f"*{ext}"):
                image_paths[str(p)] = True
            for p in Path(folder).glob(f"*{ext.upper()}"):
                image_paths[str(p)] = True
        if not image_paths:
            QMessageBox.warning(self, "警告", "该文件夹没有支持的图片文件！")
            return

        images = sorted(image_paths.keys(), key=natural_sort_key)
        image_data = []
        for img_path in images:
            w, h = self.get_image_dimensions(img_path)
            # 存储: (display_path, orig_w, orig_h, archive_path, internal_path)
            image_data.append((img_path, w, h, None, None))

        self.current_folder = folder
        self.current_archive = None
        self.image_data = image_data
        self.scroll_area.set_image_data(image_data, self.current_width)

        self.status_label.setText(f"文件夹: {os.path.basename(folder)} | 共 {len(images)} 张图片")
        self.setWindowTitle(f"漫画阅读器 - {os.path.basename(folder)}")

        saved_idx = self.restore_progress(folder)
        if saved_idx >= len(self.image_data):
            saved_idx = 0
        self.scroll_area.scroll_to_index(saved_idx)

    def load_archive(self, archive_path: str):
        """加载压缩包中的图片（支持 zip/cbz）"""
        try:
            with zipfile.ZipFile(archive_path, 'r') as zf:
                # 收集所有支持的图片文件
                image_names = []
                for name in zf.namelist():
                    ext = os.path.splitext(name)[1].lower()
                    if ext in SUPPORTED_EXT:
                        image_names.append(name)
                if not image_names:
                    QMessageBox.warning(self, "警告", "压缩包中没有支持的图片文件！")
                    return
                image_names.sort(key=natural_sort_key)

                # 获取每张图片的尺寸（从压缩包读取头部）
                image_data = []
                for internal_path in image_names:
                    with zf.open(internal_path) as f:
                        # 只读取少量数据用于尺寸识别（通常头部足够）
                        header = f.read(1024 * 10)  # 读取前10KB
                        w, h = self.get_image_dimensions_from_data(header)
                        if w == 0 or h == 0:
                            # 如果头部不够，尝试读取全部数据（极少数情况）
                            f.seek(0)
                            data = f.read()
                            w, h = self.get_image_dimensions_from_data(data)
                    display_path = f"{os.path.basename(archive_path)}/{os.path.basename(internal_path)}"
                    image_data.append((display_path, w, h, archive_path, internal_path))

            self.current_folder = None
            self.current_archive = archive_path
            self.image_data = image_data
            self.scroll_area.set_image_data(image_data, self.current_width)

            self.status_label.setText(f"压缩包: {os.path.basename(archive_path)} | 共 {len(image_data)} 张图片")
            self.setWindowTitle(f"漫画阅读器 - {os.path.basename(archive_path)}")

            saved_idx = self.restore_progress(archive_path)
            if saved_idx >= len(self.image_data):
                saved_idx = 0
            self.scroll_area.scroll_to_index(saved_idx)

        except Exception as e:
            logging.error(f"加载压缩包失败: {e}")
            QMessageBox.critical(self, "错误", f"无法读取压缩包：{str(e)}")

    def refresh_current(self):
        """刷新当前漫画（文件夹或压缩包）"""
        if self.current_folder:
            self.load_folder(self.current_folder)
        elif self.current_archive:
            self.load_archive(self.current_archive)
        else:
            QMessageBox.information(self, "提示", "请先打开一个漫画文件夹或压缩包。")

    def exit_current(self):
        """退出当前漫画，清空数据"""
        if not self.current_folder and not self.current_archive:
            return
        reply = QMessageBox.question(self, "确认", "确定退出当前漫画吗？", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.current_folder = None
            self.current_archive = None
            self.image_data = []
            self.scroll_area.set_image_data([], self.current_width)
            self.status_label.setText("未选择漫画")
            self.setWindowTitle("漫画阅读器")
            self.save_current_progress()  # 会清空进度记录

    # ---------- 用户交互 ----------
    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择漫画文件夹", "")
        if folder:
            self.load_folder(folder)

    def open_archive(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "打开压缩包", "",
            "漫画压缩包 (*.zip *.cbz);;所有文件 (*)"
        )
        if file_path:
            self.load_archive(file_path)

    def adjust_image_width(self, width: int):
        if width == self.current_width:
            return
        self.current_width = width
        self.config['width'] = width
        self.save_config()
        self.scroll_area.set_width(width, keep_position=True)

    def jump_to_index(self, index: int) -> bool:
        if not 0 <= index < len(self.image_data):
            QMessageBox.warning(self, "警告", f"页码无效，范围: 1-{len(self.image_data)}")
            return False
        self.scroll_area.scroll_to_index(index)
        return True

    def jump_to_first(self):
        if not self.image_data:
            return
        if QMessageBox.question(self, "确认", "确定跳转到首页吗？", QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.jump_to_index(0)

    def jump_to_last(self):
        if not self.image_data:
            return
        if QMessageBox.question(self, "确认", "确定跳转到尾页吗？", QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.jump_to_index(len(self.image_data) - 1)

    def jump_by_page(self):
        if not self.image_data:
            return
        text = self.page_input.text().strip()
        if not text:
            return
        try:
            page = int(text) - 1
            if self.jump_to_index(page):
                self.page_input.clear()
        except ValueError:
            QMessageBox.warning(self, "警告", "请输入有效的数字页码")

    def jump_by_filename(self):
        if not self.image_data:
            return
        filename = self.filename_input.text().strip()
        if not filename:
            return
        for idx, (display_path, _, _, _, _) in enumerate(self.image_data):
            if os.path.basename(display_path) == filename:
                if self.jump_to_index(idx):
                    self.filename_input.clear()
                return
        QMessageBox.warning(self, "警告", f"未找到文件: {filename}")

    def eventFilter(self, obj, event):
        if obj == self and event.type() == QEvent.Close:
            self.save_current_progress()
        return super().eventFilter(obj, event)

def main():
    app = QApplication(sys.argv)
    reader = ComicReader()
    reader.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()