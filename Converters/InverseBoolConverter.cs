using System.Globalization;
using System.Windows.Data;

namespace Launcher.Converters;

/// <summary>
/// 布尔取反：true -> false，false -> true。用于忙碌状态下反向控制按钮可用性。
/// </summary>
public class InverseBoolConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        => value is bool b ? !b : true;

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => value is bool b ? !b : true;
}
