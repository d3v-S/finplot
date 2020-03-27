# -*- coding: utf-8 -*-
'''
Financial data plotter with better defaults, api, behavior and performance than
mpl_finance and plotly.

Lines up your time-series with a shared X-axis; ideal for volume, RSI, etc.

Zoom does something similar to what you'd normally expect for financial data,
where the Y-axis is auto-scaled to highest high and lowest low in the active
region.
'''

from datetime import datetime
from decimal import Decimal
from functools import partial, partialmethod
from math import log10, floor, fmod
import numpy as np
import pandas as pd
import pyqtgraph as pg
from pyqtgraph import QtCore, QtGui


legend_border_color = '#000000dd'
legend_fill_color   = '#00000055'
legend_text_color   = '#dddddd66'
soft_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
hard_colors = ['#000000', '#772211', '#000066', '#555555', '#0022cc', '#ffcc00']
foreground = '#000000'
background = '#ffffff'
hollow_brush_color = background
candle_bull_color = '#26a69a'
candle_bear_color = '#ef5350'
volume_bull_color = '#92d2cc'
volume_bear_color = '#f7a9a7'
odd_plot_background = '#f0f0f0'
band_color = '#ddbbaa'
cross_hair_color = '#00000077'
draw_line_color = '#000000'
draw_done_color = '#555555'
significant_decimals = 8
significant_eps = 1e-8
v_zoom_padding = 0.03 # padded on top+bottom of plot
max_zoom_points = 20 # number of visible candles at maximum zoom
top_graph_scale = 3
clamp_grid = True
lod_candles = 3000
lod_labels = 700
y_label_width = 65

windows = [] # no gc
timers = [] # no gc
sounds = {} # no gc
plotdf2df = {} # for pandas df.plot
epoch_period2 = 1e30
last_ax = None # always assume we want to plot in the last axis, unless explicitly specified



class EpochAxisItem(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def tickStrings(self, values, scale, spacing):
        return [_epoch2local(value) for value in values]



class PandasDataSource:
    '''Candle sticks: create with five columns: time, open, close, hi, lo - in that order.
       Volume bars: create with three columns: time, open, close, volume - in that order.
       For all other types, time needs to be first, usually followed by one or more Y-columns.'''
    def __init__(self, df):
        if type(df.index) == pd.DatetimeIndex:
            df = df.reset_index()
        self.df = df.copy()
        # manage time column
        if _has_timecol(self.df):
            timecol = self.df.columns[0]
            self.df[timecol] = _pdtime2epoch(df[timecol])
            self.standalone = _is_standalone(self.df[timecol])
            self.col_data_offset = 1 # no. of preceeding columns for other plots and time column
        else:
            self.standalone = False
            self.col_data_offset = 0 # no. of preceeding columns for other plots and time column
        # setup data for joining data sources and zooming
        self.scale_cols = [i for i in range(self.col_data_offset,len(self.df.columns)) if self.df[self.df.columns[i]].dtype!=object]
        self.cache_hilo_query = ''
        self.cache_hilo_answer = None
        self.renames = {}

    @property
    def period(self):
        timecol = self.df.columns[0]
        return self.df[timecol].iloc[-1] - self.df[timecol].iloc[-2]

    @property
    def x(self):
        timecol = self.df.columns[0]
        return self.df[timecol]

    @property
    def y(self):
        col = self.df.columns[self.col_data_offset]
        return self.df[col]

    @property
    def z(self):
        col = self.df.columns[self.col_data_offset+1]
        return self.df[col]

    def calc_significant_decimals(self):
        absdiff = self.y.diff().abs()
        absdiff[absdiff<1e-30] = 1e30
        smallest_diff = absdiff.min()
        s = '%.0e' % smallest_diff
        exp = -int(s.partition('e')[2])
        decimals = max(1, min(10, exp))
        return decimals, smallest_diff

    def update_init_x(self, init_steps):
        self.init_x0 = self.get_time(offset_from_end=init_steps, period=-0.5)
        self.init_x1 = self.get_time(offset_from_end=0, period=+0.5)

    def closest_time(self, t):
        t0,_,_,_,_ = self._hilo(t, t+self.period)
        return t0

    def addcols(self, datasrc):
        self.scale_cols += [c+len(self.df.columns)-1 for c in datasrc.scale_cols]
        orig_col_data_cnt = len(self.df.columns)
        if _has_timecol(datasrc.df):
            timecol = self.df.columns[0]
            df = self.df.set_index(timecol)
            timecol = timecol if timecol in datasrc.df.columns else datasrc.df.columns[0]
            newcols = datasrc.df.set_index(timecol)
        else:
            df = self.df
            newcols = datasrc.df
        cols = list(newcols.columns)
        for i,col in enumerate(cols):
            old_col = col
            while col in self.df.columns:
                cols[i] = col = str(col)+'+'
            if old_col != col:
                datasrc.renames[old_col] = col
        newcols.columns = cols
        self.df = pd.concat([df, newcols], axis=1)
        if _has_timecol(datasrc.df):
            self.df.reset_index(inplace=True)
        datasrc.df = self.df # they are the same now
        datasrc.col_data_offset = orig_col_data_cnt
        self.cache_hilo_query = ''
        self.cache_hilo_answer = None

    def update(self, datasrc):
        orig_cols = list(self.df.columns)
        timecol = orig_cols[0]
        df = self.df.set_index(timecol)
        data = datasrc.df.set_index(timecol)
        data.columns = [self.renames.get(col, col) for col in data.columns]
        for col in df.columns:
            if col not in data.columns:
                data[col] = df[col]
        data = data.reset_index()
        self.df = data[orig_cols]

    def get_time(self, offset_from_end=0, period=0):
        '''Return timestamp of offset *from end*.'''
        if offset_from_end >= len(self.df):
            offset_from_end = len(self.df)-1
        timecol = self.df.columns[0]
        t = self.df[timecol].iloc[-1-offset_from_end]
        if period:
            t += period * self.period
        return t

    def hilo(self, x0, x1):
        '''Return five values in time range: t0, t1, highest, lowest, number of rows.'''
        query = '%.9g,%.9g' % (x0,x1)
        if query != self.cache_hilo_query:
            self.cache_hilo_query = query
            self.cache_hilo_answer = self._hilo(x0, x1)
        return self.cache_hilo_answer

    def _hilo(self, x0, x1):
        df = self.df
        timecol = df.columns[0]
        df = df.loc[((df[timecol]>=x0)&(df[timecol]<=x1)), :]
        if not len(df):
            return 0,0,0,0,0
        t0 = df[timecol].iloc[0]
        t1 = df[timecol].iloc[-1]
        valcols = df.columns[self.scale_cols]
        hi = df[valcols].max().max()
        lo = df[valcols].min().min()
        pad = (hi-lo) * v_zoom_padding
        pad = max(pad, 2e-7) # some very weird bug where too small scale stops rendering
        hi = min(hi+pad, +1e99)
        lo = max(lo-pad, -1e99)
        return t0,t1,hi,lo,len(df)

    def bear_rows(self, colcnt, x0, x1, yscale):
        df = self.df
        timecol = df.columns[0]
        opencol = df.columns[self.col_data_offset]
        closecol = df.columns[self.col_data_offset+1]
        in_timerange = (df[timecol]>=x0) & (df[timecol]<=x1)
        is_down = df[opencol] > df[closecol] # open higher than close = goes down
        df = df.loc[in_timerange&is_down]
        return self._rows(df, colcnt, yscale=yscale)

    def bull_rows(self, colcnt, x0, x1, yscale):
        df = self.df
        timecol = df.columns[0]
        opencol = df.columns[self.col_data_offset]
        closecol = df.columns[self.col_data_offset+1]
        in_timerange = (df[timecol]>=x0) & (df[timecol]<=x1)
        is_up = df[opencol] <= df[closecol] # open lower than close = goes up
        df = df.loc[in_timerange&is_up]
        return self._rows(df, colcnt, yscale=yscale)

    def rows(self, colcnt, x0, x1, yscale):
        df = self.df
        timecol = df.columns[0]
        in_timerange = (df[timecol]>=x0) & (df[timecol]<=x1)
        df = df.loc[in_timerange]
        return self._rows(df, colcnt, yscale=yscale)

    def _rows(self, df, colcnt, yscale):
        if len(df) > lod_candles:
            df = df.iloc[::len(df)//lod_candles]
        colcnt -= 1 # time is always implied
        cols = list(df.columns[self.col_data_offset:self.col_data_offset+colcnt])
        cols = [df[c] for c in cols]
        if yscale == 'log':
            for i in range(len(cols)):
                cols[i] = np.log10(cols[i])
        cols = [df[df.columns[0]]] + cols
        return zip(*cols)

    def __eq__(self, other):
        return id(self) == id(other) or id(self.df) == id(other.df)



class PlotDf(object):
    '''This class is for allowing you to do df.plot(...), as you normally would in Pandas.'''
    def __init__(self, df):
        global plotdf2df
        plotdf2df[self] = df
    def __getattribute__(self, name):
        if name == 'plot':
            return partial(dfplot, plotdf2df[self])
        return getattr(plotdf2df[self], name)
    def __getitem__(self, i):
        return plotdf2df[self].__getitem__(i)
    def __setitem__(self, i, v):
        return plotdf2df[self].__setitem__(i, v)



class FinCrossHair:
    def __init__(self, ax, color):
        self.ax = ax
        self.x = 0
        self.y = 0
        self.clamp_x = 0
        self.clamp_y = 0
        self.infos = []
        pen = pen=pg.mkPen(color=color, style=QtCore.Qt.CustomDashLine, dash=[7, 7])
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        self.xtext = pg.TextItem(color=color, anchor=(0,1))
        self.ytext = pg.TextItem(color=color, anchor=(0,0))
        self.vline.setZValue(50)
        self.hline.setZValue(50)
        self.xtext.setZValue(50)
        self.ytext.setZValue(50)
        ax.addItem(self.vline, ignoreBounds=True)
        ax.addItem(self.hline, ignoreBounds=True)
        ax.addItem(self.xtext, ignoreBounds=True)
        ax.addItem(self.ytext, ignoreBounds=True)

    def update(self, point=None):
        if point is not None:
            self.x,self.y = x,y = point.x(),point.y()
        else:
            x,y = self.x,self.y
        x,y = _clamp_xy(self.ax, x,y)
        if x == self.clamp_x and y == self.clamp_y:
            return
        self.clamp_x,self.clamp_y = x,y
        self.vline.setPos(x)
        self.hline.setPos(y)
        self.xtext.setPos(x, y)
        self.ytext.setPos(x, y)
        xtext = _epoch2local(x, clamp_grid)
        if self.ax.vb.yscale == 'log':
            y = 10**y
        rng = self.ax.vb.y_max - self.ax.vb.y_min
        rngmax = abs(self.ax.vb.y_min) + rng # any approximation is fine
        sd,se = (self.ax.significant_decimals,self.ax.significant_eps) if clamp_grid else (significant_decimals,significant_eps)
        ytext = _round_to_significant(rng, rngmax, y, sd, se)
        far_right = self.ax.viewRect().x() + self.ax.viewRect().width()*0.9
        far_bottom = self.ax.viewRect().y() + self.ax.viewRect().height()*0.1
        close2right = x > far_right
        close2bottom = y < far_bottom
        space = '      '
        if close2right:
            rxtext = xtext + space
            rytext = ytext + space
            xanchor = [1,1]
            yanchor = [1,0]
        else:
            rxtext = space + xtext
            rytext = space + ytext
            xanchor = [0,1]
            yanchor = [0,0]
        if close2bottom:
            rytext = ytext + space
            yanchor = [1,1]
            if close2right:
                xanchor = [1,2]
        self.xtext.setAnchor(xanchor)
        self.ytext.setAnchor(yanchor)
        for info in self.infos:
            rxtext,rytext = info(x,y,rxtext,rytext)
        self.xtext.setText(rxtext)
        self.ytext.setText(rytext)



class FinLegendItem(pg.LegendItem):
    def __init__(self, border_color, fill_color, **kwargs):
        super().__init__(**kwargs)
        self.layout.setVerticalSpacing(2)
        self.layout.setHorizontalSpacing(20)
        self.layout.setContentsMargins(2, 2, 10, 2)
        self.border_color = border_color
        self.fill_color = fill_color

    def paint(self, p, *args):
        p.setPen(pg.mkPen(self.border_color))
        p.setBrush(pg.mkBrush(self.fill_color))
        p.drawRect(self.boundingRect())



class FinPolyLine(pg.PolyLineROI):
    def __init__(self, vb, *args, **kwargs):
        self.vb = vb # init before parent constructor
        self.texts = []
        super().__init__(*args, **kwargs)

    def addSegment(self, h1, h2, index=None):
        super().addSegment(h1, h2, index)
        text = pg.TextItem(color=draw_line_color)
        text.setZValue(50)
        text.segment = self.segments[-1 if index is None else index]
        if index is None:
            self.texts.append(text)
        else:
            self.texts.insert(index, text)
        self.update_text(text)
        self.vb.addItem(text, ignoreBounds=True)

    def removeSegment(self, seg):
        super().removeSegment(seg)
        for text in list(self.texts):
            if text.segment == seg:
                self.vb.removeItem(text)
                self.texts.remove(text)

    def update_text(self, text):
        h0 = text.segment.handles[0]['item']
        h1 = text.segment.handles[1]['item']
        diff = h1.pos() - h0.pos()
        if diff.y() < 0:
            text.setAnchor((0.5,0))
        else:
            text.setAnchor((0.5,1))
        text.setPos(h1.pos())
        text.setText(_draw_line_segment_text(self, text.segment, h0.pos(), h1.pos()))

    def update_texts(self):
        for text in self.texts:
            self.update_text(text)

    def movePoint(self, handle, pos, modifiers=QtCore.Qt.KeyboardModifier(), finish=True, coords='parent'):
        super().movePoint(handle, pos, modifiers, finish, coords)
        self.update_texts()



class FinLine(pg.GraphicsObject):
    def __init__(self, points, pen):
        super().__init__()
        self.points = points
        self.pen = pen

    def paint(self, p, *args):
        p.setPen(self.pen)
        p.drawLine(QtCore.QPointF(*self.points[0]), QtCore.QPointF(*self.points[1]))

    def boundingRect(self):
        return QtCore.QRectF(*self.points[0], *self.points[1])


class FinViewBox(pg.ViewBox):
    def __init__(self, win, init_steps=300, yscale='linear', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.win = win
        self.yscale = yscale
        self.y_max = None
        self.y_min = None
        self.y_positive = True
        self.force_range_update = 0
        self.lines = []
        self.draw_line = None
        self.drawing = False
        self.set_datasrc(None)
        self.setMouseEnabled(x=True, y=False)
        self.init_steps = init_steps

    def set_datasrc(self, datasrc):
        self.datasrc = datasrc
        if not self.datasrc:
            return
        datasrc.update_init_x(self.init_steps)
        x0,x1,hi,lo,cnt = self.datasrc.hilo(datasrc.init_x0, datasrc.init_x1)
        if cnt >= max_zoom_points:
            self.set_range(x0, lo, x1, hi, pad=True)

    def wheelEvent(self, ev, axis=None):
        scale_fact = 1.02 ** (ev.delta() * self.state['wheelScaleFactor'])
        vr = self.targetRect()
        center = pg.Point(pg.functions.invertQTransform(self.childGroup.transform()).map(ev.pos()))
        if (center.x()-vr.left())/vr.width() < 0.05: # zoom to far left => all the way left
            center = pg.Point(vr.left(), center.y())
        elif (center.x()-vr.left())/vr.width() > 0.95: # zoom to far right => all the way right
            center = pg.Point(vr.right(), center.y())
        self.zoom_rect(vr, scale_fact, center)
        # update crosshair
        _mouse_moved(self.win, None)
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):
        if not self.datasrc:
            return
        if ev.button() != QtCore.Qt.LeftButton or ev.modifiers() != QtCore.Qt.ControlModifier:
            super().mouseDragEvent(ev, axis)
            if ev.isFinish() or self.drawing:
                self.refresh_all_y_zoom()
            if not self.drawing:
                return
        if self.draw_line and not self.drawing:
            self.set_draw_line_color(draw_done_color)
        p1 = ev.pos()
        p1 = pg.Point(pg.functions.invertQTransform(self.childGroup.transform()).map(p1))
        p1 = _clamp_point(self.parent(), p1)
        if not self.drawing:
            # add new line
            p0 = ev.lastPos()
            p0 = pg.Point(pg.functions.invertQTransform(self.childGroup.transform()).map(p0))
            p0 = _clamp_point(self.parent(), p0)
            self.draw_line = FinPolyLine(self, [p0, p1], closed=False, pen=pg.mkPen(draw_line_color), movable=False)
            self.draw_line.setZValue(40)
            self.lines.append(self.draw_line)
            self.addItem(self.draw_line)
            self.drawing = True
        else:
            # draw placed point at end of poly-line
            self.draw_line.movePoint(-1, p1)
        if ev.isFinish():
            self.drawing = False
        ev.accept()

    def mouseClickEvent(self, ev):
        if ev.button() != QtCore.Qt.LeftButton or ev.modifiers() != QtCore.Qt.ControlModifier or not self.draw_line:
            return super().mouseClickEvent(ev)
        # add another segment to the currently drawn line
        p = ev.pos()
        p = pg.Point(pg.functions.invertQTransform(self.childGroup.transform()).map(p))
        p = _clamp_point(self.parent(), p)
        self.append_draw_segment(p)
        self.drawing = False
        ev.accept()

    def keyPressEvent(self, ev):
        if _key_pressed(self, ev):
            ev.accept()
        else:
            super().keyPressEvent(ev)

    def linkedViewChanged(self, view, axis):
        if not self.datasrc:
            return
        if view:
            tr = self.targetRect()
            vr = view.targetRect()
            period = self.datasrc.period
            is_dirty = view.force_range_update > 0
            if is_dirty or abs(vr.left()-tr.left()) >= period or abs(vr.right()-tr.right()) >= period:
                if is_dirty:
                    view.force_range_update -= 1
                x0,x1,hi,lo,cnt = self.datasrc.hilo(vr.left(), vr.right())
                self.set_range(vr.left(), lo, vr.right(), hi)

    def zoom_rect(self, vr, scale_fact, center):
        if not self.datasrc:
            return
        x0 = center.x() + (vr.left()-center.x()) * scale_fact
        x1 = center.x() + (vr.right()-center.x()) * scale_fact
        self.update_y_zoom(x0, x1)

    def pan_x(self, steps=None, percent=None):
        if steps is None:
            steps = int(percent/100*self.targetRect().width())
        tr = self.targetRect()
        x1 = tr.right() + steps
        xarr = _create_series(self.datasrc.x)
        startx = xarr.iloc[0]
        endx = xarr.iloc[-1]
        if x1 > endx:
            x1 = endx
        x0 = x1 - self.targetRect().width() + 1
        if x0 < startx:
            x0 = startx
            x1 = x0 + self.targetRect().width() - 1
        self.update_y_zoom(x0, x1)

    def refresh_all_y_zoom(self):
        '''This updates Y zoom on all views, such as when a mouse drag is completed.'''
        main_vb = self
        if self.linkedView(0):
            self.force_range_update = 1 # main need to update only once to us
            main_vb = list(self.win.ci.items)[0].vb
        main_vb.force_range_update = len(self.win.ci.items)-1 # update main as many times as there are other rows
        self.update_y_zoom()
        # refresh crosshair when done
        _mouse_moved(self.win, None)

    def update_y_zoom(self, x0=None, x1=None):
        if x0 is None or x1 is None:
            tr = self.targetRect()
            x0 = tr.left()
            x1 = tr.right()
        x0,x1,hi,lo,cnt = self.datasrc.hilo(x0, x1)
        if cnt < max_zoom_points:
            return
        self.set_range(x0, lo, x1, hi, pad=True)

    def set_range(self, x0, y0, x1, y1, pad=False):
        if np.isnan(y0) or np.isnan(y1):
            return
        if pad:
            x0 -= self.datasrc.period*0.5
            x1 += self.datasrc.period*0.5
        if self.yscale == 'log':
            y0 = np.log10(y0) if y0 > 0 else -1
            y1 = np.log10(y1) if y1 > 0 else -1
        self.setRange(QtCore.QRectF(pg.Point(x0, y0), pg.Point(x1, y1)), padding=0)

    def remove_last_line(self):
        if self.lines:
            h = self.lines[-1].handles[-1]['item']
            self.lines[-1].removeHandle(h)
            if not self.lines[-1].segments:
                self.removeItem(self.lines[-1])
                self.lines = self.lines[:-1]
                self.draw_line = None
            if self.lines:
                self.draw_line = self.lines[-1]
                self.set_draw_line_color(draw_line_color)
            return True

    def append_draw_segment(self, p):
        h0 = self.draw_line.handles[-1]['item']
        h1 = self.draw_line.addFreeHandle(p)
        self.draw_line.addSegment(h0, h1)
        self.drawing = True

    def set_draw_line_color(self, color):
        if self.draw_line:
            pen = pg.mkPen(color)
            for segment in self.draw_line.segments:
                segment.currentPen = segment.pen = pen
                segment.update()

    def suggestPadding(self, axis):
        return 0



class FinPlotItem(pg.GraphicsObject):
    def __init__(self, datasrc):
        super().__init__()
        self.datasrc = datasrc
        self.picture = QtGui.QPicture()
        self.painter = QtGui.QPainter()
        self.dirty = True
        # generate picture
        visibleRect = QtCore.QRectF(self.datasrc.init_x0, 0, self.datasrc.init_x1-self.datasrc.init_x0, 0)
        self._generate_picture(visibleRect)

    def repaint(self):
        self.dirty = True
        self.paint(self.painter)

    def paint(self, p, *args):
        viewRect = self.viewRect()
        self.update_dirty_picture(viewRect)
        p.drawPicture(0, 0, self.picture)

    def update_dirty_picture(self, visibleRect):
        if self.dirty or \
            visibleRect.left() < self.cachedRect.left() or \
            visibleRect.right() > self.cachedRect.right() or \
            visibleRect.width() < self.cachedRect.width() / 3: # optimize when zooming in
            self._generate_picture(visibleRect)

    def _generate_picture(self, boundingRect):
        w = boundingRect.width()
        self.cachedRect = QtCore.QRectF(boundingRect.left()-w, 0, 3*w, 0)
        self.generate_picture(self.cachedRect)
        self.dirty = False

    def boundingRect(self):
        return QtCore.QRectF(self.picture.boundingRect())



class CandlestickItem(FinPlotItem):
    def __init__(self, ax, datasrc, draw_body=True, draw_shadow=True, candle_width=0.6):
        self.ax = ax
        self.bull_color = candle_bull_color
        self.bull_frame_color = candle_bull_color
        self.bull_body_color = hollow_brush_color
        self.bear_color = candle_bear_color
        self.bear_frame_color = candle_bear_color
        self.bear_body_color = candle_bear_color
        self.draw_body = draw_body
        self.draw_shadow = draw_shadow
        self.candle_width = candle_width
        super().__init__(datasrc)

    def generate_picture(self, boundingRect):
        w = self.datasrc.period * self.candle_width
        w2 = w * 0.5
        left,right = boundingRect.left(), boundingRect.right()
        p = self.painter
        p.begin(self.picture)
        rows = list(self.datasrc.bear_rows(5, left, right, yscale=self.ax.vb.yscale))
        if self.draw_shadow:
            p.setPen(pg.mkPen(self.bear_color))
            for t,open,close,high,low in rows:
                if high > low:
                    p.drawLine(QtCore.QPointF(t, low), QtCore.QPointF(t, high))
        if self.draw_body:
            p.setPen(pg.mkPen(self.bear_frame_color))
            p.setBrush(pg.mkBrush(self.bear_body_color))
            for t,open,close,high,low in rows:
                p.drawRect(QtCore.QRectF(t-w2, open, w, close-open))
        rows = list(self.datasrc.bull_rows(5, left, right, yscale=self.ax.vb.yscale))
        if self.draw_shadow:
            p.setPen(pg.mkPen(self.bull_color))
            for t,open,close,high,low in rows:
                if high > low:
                    p.drawLine(QtCore.QPointF(t, low), QtCore.QPointF(t, high))
        if self.draw_body:
            p.setPen(pg.mkPen(self.bull_frame_color))
            p.setBrush(pg.mkBrush(self.bull_body_color))
            for t,open,close,high,low in rows:
                p.drawRect(QtCore.QRectF(t-w2, open, w, close-open))
        p.end()



class VolumeItem(FinPlotItem):
    def __init__(self, ax, datasrc, candle_width=0.6):
        self.ax = ax
        self.bull_color = volume_bull_color
        self.bear_color = volume_bear_color
        self.candle_width = candle_width
        super().__init__(datasrc)

    def generate_picture(self, boundingRect):
        w = self.datasrc.period * self.candle_width
        w2 = w * 0.5
        left,right = boundingRect.left(), boundingRect.right()
        p = self.painter
        p.begin(self.picture)
        p.setPen(pg.mkPen(self.bear_color))
        p.setBrush(pg.mkBrush(self.bear_color))
        for t,open,close,volume in self.datasrc.bear_rows(4, left, right, yscale=self.ax.vb.yscale):
            p.drawRect(QtCore.QRectF(t-w2, 0, w, volume))
        p.setPen(pg.mkPen(self.bull_color))
        p.setBrush(pg.mkBrush(self.bull_color))
        for t,open,close,volume in self.datasrc.bull_rows(4, left, right, yscale=self.ax.vb.yscale):
            p.drawRect(QtCore.QRectF(t-w2, 0, w, volume))
        p.end()



class ScatterLabelItem(FinPlotItem):
    def __init__(self, datasrc, color, anchor):
        self.color = color
        self.text_items = {}
        self.anchor = anchor
        self.show = False
        super().__init__(datasrc)

    def generate_picture(self, bounding_rect):
        rows = self.getrows(bounding_rect)
        rows = [(t,y,txt) for t,y,txt in rows if txt]
        if len(rows) > lod_labels: # don't even generate when there's too many of them
            self.clear_items(list(self.text_items.keys()))
            return
        drops = set(self.text_items.keys())
        created = 0
        for t,y,txt in rows:
            txt = str(txt)
            key = '%s:%.8f' % (t, y)
            if key in self.text_items:
                item = self.text_items[key]
                item.setText(txt)
                drops.remove(key)
            else:
                self.text_items[key] = item = pg.TextItem(txt, color=self.color, anchor=self.anchor)
                item.setPos(t, y)
                item.setParentItem(self)
                created += 1
        if created > 0 or self.dirty: # only reduce cache if we've added some new or updated
            self.clear_items(drops)

    def clear_items(self, drop_keys):
        for key in drop_keys:
            item = self.text_items[key]
            item.scene().removeItem(item)
            del self.text_items[key]

    def getrows(self, bounding_rect):
        left,right = bounding_rect.left(), bounding_rect.right()
        rows = [(t,y,txt) for t,y,txt in self.datasrc.rows(3, left, right, yscale='linear') if txt]
        return rows

    def boundingRect(self):
        return self.viewRect()


def create_plot(title='Finance Plot', rows=1, init_zoom_periods=1e10, maximize=True, yscale='linear'):
    global windows, v_zoom_padding, last_ax
    if yscale == 'log':
        v_zoom_padding = 0.0
    pg.setConfigOptions(foreground=foreground, background=background)
    win = pg.GraphicsWindow(title=title)
    windows.append(win)
    if maximize:
        win.showMaximized()
    win.ci.setContentsMargins(0, 0, 0 ,0)
    win.ci.setSpacing(0)
    # normally first graph is of higher significance, so enlarge
    win.ci.layout.setRowStretchFactor(0, top_graph_scale)
    axs = []
    prev_ax = None
    for n in range(rows):
        viewbox = FinViewBox(win, init_steps=init_zoom_periods, yscale=yscale)
        ax = prev_ax = _add_timestamp_plot(win, prev_ax, viewbox=viewbox, index=n, yscale=yscale)
        _set_plot_x_axis_leader(ax)
        if n == 0:
            viewbox.setFocus()
        axs += [ax]
    win.proxy_mmove = pg.SignalProxy(win.scene().sigMouseMoved, rateLimit=144, slot=partial(_mouse_moved, win))
    win._last_mouse_evs = None
    win._last_mouse_y = 0
    last_ax = axs[0]
    if len(axs) == 1:
        return axs[0]
    return axs


def candlestick_ochl(datasrc, draw_body=True, draw_shadow=True, candle_width=0.6, ax=None):
    ax = _create_plot(ax=ax, maximize=False)
    datasrc = _create_datasrc(datasrc)
    datasrc.scale_cols = [3,4] # only hi+lo scales
    _set_datasrc(ax, datasrc)
    item = CandlestickItem(ax=ax, datasrc=datasrc, draw_body=draw_body, draw_shadow=draw_shadow, candle_width=candle_width)
    ax.significant_decimals,ax.significant_eps = datasrc.calc_significant_decimals()
    item.ax = ax
    item.update_data = partial(_update_data, item)
    ax.addItem(item)
    # item.setZValue(20)
    _pre_process_data(item)
    return item


def volume_ocv(datasrc, candle_width=0.6, ax=None):
    ax = _create_plot(ax=ax, maximize=False)
    datasrc = _create_datasrc(datasrc)
    datasrc.scale_cols = [3] # only volume scales
    _set_datasrc(ax, datasrc)
    item = VolumeItem(ax=ax, datasrc=datasrc, candle_width=candle_width)
    item.ax = ax
    item.update_data = partial(_update_data, item)
    ax.addItem(item)
    item.setZValue(-1)
    _pre_process_data(item)
    return item


def plot(x, y=None, color=None, width=1, ax=None, style=None, legend=None, zoomscale=True):
    ax = _create_plot(ax=ax, maximize=False)
    used_color = color if color else _get_color(ax, style)
    datasrc = _create_datasrc(x, y)
    if not zoomscale:
        datasrc.scale_cols = []
    _set_datasrc(ax, datasrc)
    if legend is not None and ax.legend is None:
        ax.legend = FinLegendItem(border_color=legend_border_color, fill_color=legend_fill_color, size=None, offset=(3,2))
        ax.legend.setParentItem(ax.vb)
    if style is None or style=='-':
        connect_dots = 'finite' # same as matplotlib; use datasrc.standalone=True if you want to keep separate intervals on a plot
        item = ax.plot(datasrc.x, datasrc.y, pen=pg.mkPen(used_color, width=width), name=legend, connect=connect_dots)
    else:
        symbol = {'v':'t', '^':'t1', '>':'t2', '<':'t3'}.get(style, style) # translate some similar styles
        item = ax.plot(datasrc.x, datasrc.y, pen=None, symbol=symbol, symbolPen=None, symbolSize=10, symbolBrush=pg.mkBrush(used_color), name=legend)
        item.scatter._dopaint = item.scatter.paint
        item.scatter.paint = partial(_paint_scatter, item.scatter)
        # optimize (when having large number of points) by ignoring scatter click detection
        _dummy_mouse_click = lambda ev: 0
        item.scatter.mouseClickEvent = _dummy_mouse_click
    item.opts['handed_color'] = color
    item.ax = ax
    item.datasrc = datasrc
    # check if no epsilon set yet
    if 0.99 < ax.significant_eps/significant_eps < 1.01:
        ax.significant_decimals,ax.significant_eps = datasrc.calc_significant_decimals()
    item.update_data = partial(_update_data, item)
    _pre_process_data(item)
    if ax.legend is not None:
        for _,label in ax.legend.items:
            label.setAttr('justify', 'left')
            label.setText(label.text, color=legend_text_color)
    return item


def labels(x, y=None, labels=None, color=None, ax=None, anchor=(0.5,1)):
    ax = _create_plot(ax=ax, maximize=False)
    color = color if color else '#000000'
    datasrc = _create_datasrc(x, y, labels)
    datasrc.scale_cols = [] # don't use this for scaling
    _set_datasrc(ax, datasrc)
    item = ScatterLabelItem(datasrc=datasrc, color=color, anchor=anchor)
    item.ax = ax
    item.update_data = partial(_update_data, item)
    ax.addItem(item)
    _pre_process_data(item)
    return item


def dfplot(df, x=None, y=None, color=None, width=1, ax=None, style=None, legend=None, zoomscale=True):
    legend = legend if legend else y
    x = x if x else df.columns[0]
    y = y if y else df.columns[1]
    return plot(df[x], df[y], color=color, width=width, ax=ax, style=style, legend=legend, zoomscale=zoomscale)


def set_y_range(ax, ymin, ymax):
    ax.setLimits(yMin=ymin, yMax=ymax)


def set_yscale(ax, yscale='linear'):
    ax.setLogMode(y=(yscale=='log'))
    ax.vb.yscale = yscale


def add_band(ax, y0, y1, color=band_color):
    lr = pg.LinearRegionItem([y0,y1], orientation=pg.LinearRegionItem.Horizontal, brush=pg.mkBrush(color), movable=False)
    lr.lines[0].setPen(pg.mkPen(None))
    lr.lines[1].setPen(pg.mkPen(None))
    lr.setZValue(-10)
    ax.addItem(lr)


def add_line(p0, p1, color=draw_line_color, interactive=False, ax=None):
    ax = _create_plot(ax=ax, maximize=False)
    x_pts = _pdtime2epoch(pd.Series([p0[0], p1[0]]))
    pts = [(x_pts[0], p0[1]), (x_pts[1], p1[1])]
    if interactive:
        line = FinPolyLine(ax.vb, pts, closed=False, pen=pg.mkPen(color), movable=False)
        ax.vb.lines.append(line)
    else:
        line = FinLine(pts, pen=pg.mkPen(color))
    line.ax = ax
    ax.addItem(line)
    return line


def remove_line(line):
    ax = line.ax
    ax.removeItem(line)
    if line in ax.vb.lines:
        ax.vb.lines.remove(line)
    if hasattr(line, 'texts'):
        for txt in line.texts:
            ax.vb.removeItem(txt)


def add_text(pos, s, color=draw_line_color, anchor=(0,0), ax=None):
    ax = _create_plot(ax=ax, maximize=False)
    text = pg.TextItem(s, color=color, anchor=anchor)
    text.setPos(_pdtime2epoch(pd.Series([pos[0]]))[0], pos[1])
    text.setZValue(50)
    text.ax = ax
    ax.addItem(text, ignoreBounds=True)
    return text


def remove_text(text):
    text.ax.removeItem(text)


def set_time_inspector(inspector, ax=None):
    '''Callback when clicked like so: inspector().'''
    ax = ax if ax else last_ax
    win = ax.vb.win
    win.proxy_click = pg.SignalProxy(win.scene().sigMouseClicked, slot=partial(_time_clicked, ax, inspector))


def add_crosshair_info(ax, info):
    '''Callback when crosshair updated like so: info(x,y,xtext,ytext); the info()
       callback must return two values: xtext and ytext.'''
    ax.crosshair.infos.append(info)


def timer_callback(update_func, seconds, single_shot=False):
    global timers
    timer = QtCore.QTimer()
    timer.timeout.connect(update_func)
    if single_shot:
        timer.setSingleShot(True)
    timer.start(seconds*1000)
    timers.append(timer)


def show():
    if windows:
        QtGui.QApplication.instance().exec_()
        windows.clear()


def play_sound(filename):
    if filename not in sounds:
        from PyQt5.QtMultimedia import QSound
        sounds[filename] = QSound(filename) # disallow gc
    s = sounds[filename]
    s.play()


#################### INTERNALS ####################


def _create_plot(ax=None, **kwargs):
    if ax:
        return ax
    if last_ax:
        return last_ax
    return create_plot(**kwargs)


def _add_timestamp_plot(win, prev_ax, viewbox, index, yscale):
    if prev_ax is not None:
        prev_ax.hideAxis('bottom') # hide the whole previous axis
        win.nextRow()
    ax = pg.PlotItem(viewBox=viewbox, axisItems={'bottom': EpochAxisItem(orientation='bottom')}, name='plot-%i'%index)
    ax.axes['left']['item'].textWidth = y_label_width # this is to put all graphs on equal footing when texts vary from 0.4 to 2000000
    ax.axes['left']['item'].setStyle(tickLength=-5) # some bug, totally unexplicable (why setting the default value again would fix repaint width as axis scale down)
    ax.axes['left']['item'].setZValue(30) # put axis in front instead of behind data
    ax.axes['bottom']['item'].setZValue(30)
    ax.setLogMode(y=(yscale=='log'))
    ax.significant_decimals = significant_decimals
    ax.significant_eps = significant_eps
    ax.crosshair = FinCrossHair(ax, color=cross_hair_color)
    if index%2:
        viewbox.setBackgroundColor(odd_plot_background)
    viewbox.setParent(ax)
    win.addItem(ax)
    return ax


def _is_standalone(timeser):
    # more than N percent gaps or reversals probably means this is a standalone plot
    return timeser.isnull().sum() + (timeser.diff()<=0).sum() > len(timeser)*0.1


def _create_series(a):
    return a if isinstance(a, pd.Series) else pd.Series(a)


def _create_datasrc(*args):
    args = [a for a in args if a is not None]
    if len(args) == 1 and type(args[0]) == PandasDataSource:
        return args[0]
    if len(args) == 1 and type(args[0]) in (list, tuple):
        args = [np.array(args[0])]
    if len(args) == 1 and type(args[0]) == np.ndarray:
        args = [pd.DataFrame(args[0].T)]
    if len(args) == 1 and type(args[0]) == pd.DataFrame:
        return PandasDataSource(args[0])
    args = [_create_series(a) for a in args]
    return PandasDataSource(pd.concat(args, axis=1))


def _set_datasrc(ax, datasrc):
    viewbox = ax.vb
    if not datasrc.standalone:
        if viewbox.datasrc is None:
            viewbox.set_datasrc(datasrc) # for mwheel zoom-scaling
            _set_x_limits(ax, datasrc)
        else:
            viewbox.datasrc.addcols(datasrc)
            _set_x_limits(ax, datasrc)
            viewbox.set_datasrc(viewbox.datasrc) # update zoom
            datasrc.init_x0 = viewbox.datasrc.init_x0
            datasrc.init_x1 = viewbox.datasrc.init_x1
    else:
        datasrc.update_init_x(viewbox.init_steps)
    # update period if this datasrc has higher resolution
    global epoch_period2
    if epoch_period2 > 1e10 or not datasrc.standalone:
        ep2 = datasrc.period / 2
        if ep2 < epoch_period2:
            epoch_period2 = ep2


def _has_timecol(df):
    return len(df.columns) >= 2


def _update_data(item, ds):
    ds = _create_datasrc(ds)
    item.datasrc.update(ds)
    if isinstance(item, FinPlotItem):
        item.dirty = True
    else:
        item.setData(item.datasrc.x, item.datasrc.y)
    x_min,x1 = _set_x_limits(item.ax, item.datasrc)
    # scroll all plots if we're at the far right
    tr = item.ax.vb.targetRect()
    x0 = x1 - tr.width()
    for ax in item.ax.vb.win.ci.items:
        ax.setLimits(xMin=x_min, xMax=x1)
    if tr.right() >= x1-item.datasrc.period*5:
        for ax in item.ax.vb.win.ci.items:
            _,_,y0,y1,cnt = ax.vb.datasrc.hilo(x0, x1)
            ax.vb.set_range(x0, y0, x1, y1, pad=False)
    for ax in item.ax.vb.win.ci.items:
        ax.vb.update()


def _pre_process_data(item):
    item.ax.vb.y_max = np.nanmax(item.datasrc.y)
    item.ax.vb.y_min = np.nanmin(item.datasrc.y)
    if item.ax.vb.y_min <= 0:
        item.ax.vb.y_positive = False


def _set_plot_x_axis_leader(ax):
    '''The first plot to add some data is the leader. All other's X-axis will follow this one.'''
    if ax.vb.linkedView(0):
        return
    for _ax in ax.vb.win.ci.items:
        if not _ax.vb.linkedView(0) and _ax.vb.name != ax.vb.name:
            ax.setXLink(_ax.vb.name)
            break


def _set_x_limits(ax, datasrc):
    x0 = datasrc.get_time(1e20, period=-0.5)
    x1 = datasrc.get_time(0, period=+0.5)
    ax.setLimits(xMin=x0, xMax=x1)
    return x0, x1


def _repaint_candles():
    '''Candles are only partially drawn, and therefore needs manual dirty reminder whenever it goes off-screen.'''
    for win in windows:
        for ax in win.ci.items:
            for item in ax.items:
                if isinstance(item, FinPlotItem):
                    item.repaint()


def _paint_scatter(item, p, *args):
    with np.errstate(invalid='ignore'): # make pg's mask creation calls to numpy shut up
        item._dopaint(p, *args)


def _key_pressed(vb, ev):
    if ev.text() == 'g': # grid
        global clamp_grid
        clamp_grid = not clamp_grid
        for win in windows:
            for ax in win.ci.items:
                ax.crosshair.update()
    elif ev.text() in ('\r', ' '): # enter, space
        vb.set_draw_line_color(draw_done_color)
        vb.draw_line = None
    elif ev.text() in ('\x7f', '\b'): # del, backspace
        if not vb.remove_last_line():
            return False
    elif ev.key() == QtCore.Qt.Key_Left:
        vb.pan_x(percent=-15)
    elif ev.key() == QtCore.Qt.Key_Right:
        vb.pan_x(percent=+15)
    elif ev.key() == QtCore.Qt.Key_Home:
        vb.pan_x(steps=-1e10)
        _repaint_candles()
    elif ev.key() == QtCore.Qt.Key_End:
        vb.pan_x(steps=+1e10)
        _repaint_candles()
    elif ev.key() == QtCore.Qt.Key_Escape:
        vb.win.close()
    else:
        return False
    return True


def _mouse_moved(win, evs):
    if not evs:
        evs = win._last_mouse_evs
        if not evs:
            return
    win._last_mouse_evs = evs
    pos = evs[-1]
    # allow inter-pixel moves if moving mouse slowly
    y = pos.y()
    dy = y - win._last_mouse_y
    if 0 < abs(dy) <= 1:
        pos.setY(pos.y() - dy/2)
    win._last_mouse_y = y
    # apply to all crosshairs
    for ax in win.ci.items:
        point = ax.vb.mapSceneToView(pos)
        if ax.crosshair:
            ax.crosshair.update(point)


def _wheel_event_wrapper(self, orig_func, ev):
    # scrolling on the border is simply annoying, pop in a couple of pixels to make sure
    d = QtCore.QPoint(-2,0)
    ev = QtGui.QWheelEvent(ev.pos()+d, ev.globalPos()+d, ev.pixelDelta(), ev.angleDelta(), ev.angleDelta().y(), QtCore.Qt.Vertical, ev.buttons(), ev.modifiers())
    orig_func(self, ev)


def _time_clicked(ax, inspector, ev):
    pos = ev[0].scenePos()
    point = ax.vb.mapSceneToView(pos)
    t = point.x() - epoch_period2
    t = ax.vb.datasrc.closest_time(t)
    inspector(t, point.y())


def _get_color(ax, style):
    if style is None or style=='-':
        index = len([i for i in ax.items if isinstance(i,pg.PlotDataItem) and not i.opts['symbol'] and not i.opts['handed_color']])
        return soft_colors[index%len(soft_colors)]
    index = len([i for i in ax.items if isinstance(i,pg.PlotDataItem) and i.opts['symbol'] and not i.opts['handed_color']])
    return hard_colors[index%len(hard_colors)]


def _pdtime2epoch(t):
    if isinstance(t, pd.Series):
        if isinstance(t.iloc[0], pd.Timestamp):
            return t.astype('int64') // int(1e9)
        if np.nanmax(t.values) > 1e10: # handle ms epochs
            return t.astype('float64') / 1e3
    return t


def _epoch2local(t, strip_seconds=True):
    try:
        s = datetime.fromtimestamp(t).isoformat().replace('T',' ')
        return s.rpartition(':' if strip_seconds else '.')[0]
    except:
        return ''


def _round_to_significant(rng, rngmax, x, significant_decimals, significant_eps):
    is_highres = rng/significant_eps > 1e3 and (rngmax>1e5 or rngmax<1e-2)
    sd = significant_decimals
    if is_highres:
        exp10 = floor(log10(abs(x)))
        x = x / (10**exp10)
        sd = min(5, sd+int(log10(rngmax)))
        fmt = '%%%i.%ife%%i' % (sd, sd)
        r = fmt % (x, exp10)
    else:
        eps = fmod(x, significant_eps)
        if abs(eps) >= significant_eps/2:
            # round up
            eps -= np.sign(eps)*significant_eps
        x -= eps
        fmt = '%%%i.%if' % (sd, sd)
        r = fmt % x
    return r


def _clamp_xy(ax, x, y):
    if clamp_grid:
        x -= fmod(x+epoch_period2, epoch_period2*2) - epoch_period2
        eps = ax.significant_eps
        y -= fmod(y+eps*0.5, eps) - eps*0.5
    return x, y


def _clamp_point(ax, p):
    if clamp_grid:
        x,y = _clamp_xy(ax, p.x(), p.y())
        return pg.Point(x, y)
    return p


def _draw_line_segment_text(polyline, segment, pos0, pos1):
    diff = pos1 - pos0
    mins = int(abs(diff.x()) / 60)
    hours = mins//60
    mins = mins%60
    if hours < 24:
        ts = '%0.2i:%0.2i' % (hours, mins)
    else:
        days = hours // 24
        hours %= 24
        ts = '%id %0.2i:%0.2i' % (days, hours, mins)
    if polyline.vb.y_positive:
        y0,y1 = (10**pos0.y(),10**pos1.y()) if polyline.vb.yscale == 'log' else (pos0.y(),pos1.y())
        value = '%+.2f %%' % (100 * y1 / y0 - 100)
    else:
        dy = diff.y()
        if dy and (abs(dy) >= 1e4 or abs(dy) <= 1e-2):
            value = '%+3.3g' % dy
        else:
            value = '%+2.2f' % dy
    extra = _draw_line_extra_text(polyline, segment, pos0, pos1)
    return '%s %s (%s)' % (value, extra, ts)


def _draw_line_extra_text(polyline, segment, pos0, pos1):
    '''Shows the proportions of this line height compared to the previous segment.'''
    prev_text = None
    for text in polyline.texts:
        if prev_text is not None and text.segment == segment:
            h0 = prev_text.segment.handles[0]['item']
            h1 = prev_text.segment.handles[1]['item']
            if polyline.vb.y_positive:
                prev_change = h1.pos().y() / h0.pos().y() - 1
                this_change = pos1.y() / pos0.y() - 1
            else:
                prev_change = h1.pos().y() - h0.pos().y()
                this_change = pos1.y() - pos0.y()
            if not abs(prev_change) > 1e-8:
                break
            change_part = abs(this_change / prev_change)
            return ' = 1:%.2f ' % change_part
        prev_text = text
    return ''


# default to black-on-white
pg.widgets.GraphicsView.GraphicsView.wheelEvent = partialmethod(_wheel_event_wrapper, pg.widgets.GraphicsView.GraphicsView.wheelEvent)
# pick up win resolution
try:
    import ctypes
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    lod_candles = int(user32.GetSystemMetrics(0) * 1.6)
except:
    pass


if False: # performance measurement code
    import time, sys
    def self_timecall(self, pname, fname, func, *args, **kwargs):
        ## print('self_timecall', pname, fname)
        t0 = time.perf_counter()
        r = func(self, *args, **kwargs)
        t1 = time.perf_counter()
        print('%s.%s: %f' % (pname, fname, t1-t0))
        return r
    def timecall(fname, func, *args, **kwargs):
        ## print('timecall', fname)
        t0 = time.perf_counter()
        r = func(*args, **kwargs)
        t1 = time.perf_counter()
        print('%s: %f' % (fname, t1-t0))
        return r
    def wrappable(fn, f):
        try:    return callable(f) and str(f.__module__) == 'finplot'
        except: return False
    m = sys.modules['finplot']
    for fname in dir(m):
        func = getattr(m, fname)
        if wrappable(fname, func):
            for fname2 in dir(func):
                func2 = getattr(func, fname2)
                if wrappable(fname2, func2):
                    print(fname, str(type(func)), '->', fname2, str(type(func2)))
                    setattr(func, fname2, partialmethod(self_timecall, fname, fname2, func2))
            setattr(m, fname, partial(timecall, fname, func))
