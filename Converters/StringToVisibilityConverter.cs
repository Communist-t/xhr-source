using System.Globalization;
using System.Windows;
using System.Windows.Data;

namespace Launcher.Converters;

/// <summary>
/// 非空字符串 -> Visible；空串 / null -> Collapsed。用于状态提示文本的显隐。
/// </summary>
public class StringToVisibilityConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        => string.IsNullOrWhiteSpace(value as string) ? Visibility.Collapsed : Visibility.Visible;

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotImplementedException();
}
