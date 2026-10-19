use_frameworks!
inhibit_all_warnings!

# Same VLC 3 engine on both: TVVLCKit (Apple TV) and MobileVLCKit (iPhone), both 3.7.4.
target 'AtvPlayer' do
  platform :tvos, '17.0'
  pod 'TVVLCKit', '3.7.4'
end

target 'AtvPlayerPhone' do
  platform :ios, '17.0'
  pod 'MobileVLCKit', '3.7.4'
end

post_install do |installer|
  installer.pods_project.targets.each do |target|
    target.build_configurations.each do |config|
      config.build_settings['TVOS_DEPLOYMENT_TARGET'] = '17.0'
      config.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '17.0'
      config.build_settings['CODE_SIGNING_ALLOWED'] = 'NO'
      config.build_settings['CODE_SIGNING_REQUIRED'] = 'NO'
    end
  end
end
