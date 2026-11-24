using System.Windows;
using System.Windows.Controls;

namespace Launcher.Controls;

/// <summary>
/// 员工卡片自适应面板：列数随可用宽度变化，卡片宽度在上限内自适应。
/// 规则：
///  1. 列数 = 按 <see cref="MinCardWidth"/> 能放下的列数，且不超过卡片总数；
///  2. 列宽 = 可用宽度均分，但不超过 <see cref="MaxCardWidth"/>（避免窗口过宽时卡片被拉扁）；
///  3. 扣除卡片与间距后的剩余空间左右对称留白，不会单侧堆积。
/// 面板在每次测量时重算，窗口缩放、最大化都会立即响应。
/// </summary>
public class AdaptiveCardPanel : Panel
{
    /// <summary>卡片最小宽度，决定一列最少占多宽（即一行能放几张）。</summary>
    public static readonly DependencyProperty MinCardWidthProperty =
        DependencyProperty.Register(nameof(MinCardWidth), typeof(double), typeof(AdaptiveCardPanel),
            new FrameworkPropertyMetadata(320.0, FrameworkPropertyMetadataOptions.AffectsMeasure));

    /// <summary>卡片最大宽度，窗口很宽时卡片不会无限拉伸。</summary>
    public static readonly DependencyProperty MaxCardWidthProperty =
        DependencyProperty.Register(nameof(MaxCardWidth), typeof(double), typeof(AdaptiveCardPanel),
            new FrameworkPropertyMetadata(440.0, FrameworkPropertyMetadataOptions.AffectsMeasure));

    /// <summary>卡片之间的水平与垂直间距。</summary>
    public static readonly DependencyProperty GapProperty =
        DependencyProperty.Register(nameof(Gap), typeof(double), typeof(AdaptiveCardPanel),
            new FrameworkPropertyMetadata(24.0, FrameworkPropertyMetadataOptions.AffectsMeasure));

    public double MinCardWidth
    {
        get => (double)GetValue(MinCardWidthProperty);
        set => SetValue(MinCardWidthProperty, value);
    }

    public double MaxCardWidth
    {
        get => (double)GetValue(MaxCardWidthProperty);
        set => SetValue(MaxCardWidthProperty, value);
    }

    public double Gap
    {
        get => (double)GetValue(GapProperty);
        set => SetValue(GapProperty, value);
    }

    /// <summary>按可用宽度与卡片数量计算列数与列宽。</summary>
    private (int Columns, double CardWidth, double OffsetX) ComputeLayout(double availableWidth, int itemCount)
    {
        var min = MinCardWidth > 0 ? MinCardWidth : 320.0;
        var max = MaxCardWidth >= min ? MaxCardWidth : min;
        var gap = Gap >= 0 ? Gap : 0.0;

        // 宽度未知（ScrollViewer 未定宽）时按最小宽度单列排布，避免异常拉伸。
        if (double.IsInfinity(availableWidth) || double.IsNaN(availableWidth) || availableWidth <= 0)
            return (1, min, 0);

        // 按最小宽度能放几列
        var columns = (int)Math.Floor((availableWidth + gap) / (min + gap));
        if (columns < 1) columns = 1;
        if (columns > itemCount) columns = Math.Max(1, itemCount);

        // 列宽均分，但不超过上限
        var cardWidth = (availableWidth - gap * (columns - 1)) / columns;
        if (cardWidth > max) cardWidth = max;
        if (cardWidth < 0) cardWidth = 0;

        // 剩余空间左右对称居中
        var used = cardWidth * columns + gap * (columns - 1);
        var offsetX = Math.Max(0, (availableWidth - used) / 2.0);

        return (columns, cardWidth, offsetX);
    }

    protected override Size MeasureOverride(Size availableSize)
    {
        var count = InternalChildren.Count;
        if (count == 0) return new Size(0, 0);

        var (columns, cardWidth, _) = ComputeLayout(availableSize.Width, count);

        var rowHeight = 0.0;
        for (var i = 0; i < count; i++)
        {
            var child = InternalChildren[i];
            child.Measure(new Size(cardWidth, double.PositiveInfinity));
            if (child.DesiredSize.Height > rowHeight)
                rowHeight = child.DesiredSize.Height;
        }

        var rows = (int)Math.Ceiling(count / (double)columns);
        var totalHeight = rows * rowHeight + Math.Max(0, rows - 1) * Gap;

        var width = double.IsInfinity(availableSize.Width) || double.IsNaN(availableSize.Width)
            ? cardWidth * columns + Gap * Math.Max(0, columns - 1)
            : availableSize.Width;

        return new Size(width, totalHeight);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        var count = InternalChildren.Count;
        if (count == 0) return finalSize;

        var (columns, cardWidth, offsetX) = ComputeLayout(finalSize.Width, count);

        // 统一行高，保证同一行卡片顶部对齐、行间距一致。
        var rowHeight = 0.0;
        for (var i = 0; i < count; i++)
        {
            if (InternalChildren[i].DesiredSize.Height > rowHeight)
                rowHeight = InternalChildren[i].DesiredSize.Height;
        }

        for (var i = 0; i < count; i++)
        {
            var row = i / columns;
            var column = i % columns;
            var x = offsetX + column * (cardWidth + Gap);
            var y = row * (rowHeight + Gap);
            InternalChildren[i].Arrange(new Rect(x, y, cardWidth, rowHeight));
        }

        return finalSize;
    }
}
