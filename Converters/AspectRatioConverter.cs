using System.Globalization;
using System.Windows.Data;

namespace Launcher.Converters;

/// <summary>
/// 按宽高比把实际宽度换算成高度：height = width * Ratio（默认 2/3，对应 3:2 海报）。
/// 用于让封面框始终保持与海报一致的比例，避免裁切或拉伸。
/// </summary>
public class AspectRatioConverter : IValueConverter
{
    public double Ratio { get; set; } = 2.0 / 3.0;

    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        if (parameter is string p &&
            double.TryParse(p, NumberStyles.Float, CultureInfo.InvariantCulture, out var ratio))
        {
            Ratio = ratio;
        }

        if (value is double width && !double.IsNaN(width) && width > 0)
        {
            return width * Ratio;
        }

        return double.NaN;
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotImplementedException();
}
